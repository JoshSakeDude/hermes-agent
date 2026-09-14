"""Per-card Kanban token/cost accounting and cooperative-yield helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import subprocess
from typing import Any, Optional

BUDGET_YIELDED_OUTCOME = "budget_yielded"
MAX_BUDGET_CONTINUATIONS = 2
WARN_FRACTION = Decimal("0.75")
FINALIZATION_REQUESTS = 2


@dataclass(frozen=True)
class BudgetSnapshot:
    """One usage snapshot; cache reads stay visible but are not billable tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 0
    estimated_cost_usd: Optional[Decimal] = None

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write_tokens

    def __add__(self, other: "BudgetSnapshot") -> "BudgetSnapshot":
        if not isinstance(other, BudgetSnapshot):
            return NotImplemented
        costs = [value for value in (self.estimated_cost_usd, other.estimated_cost_usd) if value is not None]
        return BudgetSnapshot(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            request_count=self.request_count + other.request_count,
            estimated_cost_usd=sum(costs, Decimal("0")) if costs else None,
        )

    @classmethod
    def from_usage(cls, usage: Any, *, estimated_cost_usd: Any = None) -> "BudgetSnapshot":
        cost = None
        if estimated_cost_usd is not None:
            try:
                cost = Decimal(str(estimated_cost_usd))
            except (InvalidOperation, TypeError, ValueError):
                cost = None
        return cls(
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            cache_read_tokens=int(usage.cache_read_tokens or 0),
            cache_write_tokens=int(usage.cache_write_tokens or 0),
            reasoning_tokens=int(usage.reasoning_tokens or 0),
            request_count=int(getattr(usage, "request_count", 1) or 1),
            estimated_cost_usd=cost,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["billable_tokens"] = self.billable_tokens
        result["total_tokens"] = self.total_tokens
        if self.estimated_cost_usd is not None:
            result["estimated_cost_usd"] = str(self.estimated_cost_usd)
        return result


class TokenBudget:
    """Track one worker run against optional token and estimated-cost ceilings."""

    def __init__(
        self,
        max_total_tokens: Optional[int],
        *,
        max_estimated_cost_usd: Optional[Decimal] = None,
        continuation_count: int = 0,
    ) -> None:
        self.max_total_tokens = int(max_total_tokens) if max_total_tokens is not None else None
        self.max_estimated_cost_usd = (
            Decimal(str(max_estimated_cost_usd)) if max_estimated_cost_usd is not None else None
        )
        self.continuation_count = int(continuation_count)
        self._snapshot = BudgetSnapshot()
        self._warned = False
        self._finalization_allowance = 0
        self._finalization_used = 0

    @property
    def run_snapshot(self) -> BudgetSnapshot:
        return self._snapshot

    @property
    def billable_tokens(self) -> int:
        return self._snapshot.billable_tokens

    @property
    def estimated_cost_usd(self) -> Optional[Decimal]:
        return self._snapshot.estimated_cost_usd

    @property
    def remaining(self) -> Optional[int]:
        if self.max_total_tokens is None:
            return None
        return max(0, self.max_total_tokens - self.billable_tokens)

    @property
    def remaining_cost_usd(self) -> Optional[Decimal]:
        if self.max_estimated_cost_usd is None:
            return None
        return max(Decimal("0"), self.max_estimated_cost_usd - (self.estimated_cost_usd or Decimal("0")))

    @property
    def exhausted_dimension(self) -> Optional[str]:
        if self.max_total_tokens is not None and self.billable_tokens >= self.max_total_tokens:
            return "billable_tokens"
        if (
            self.max_estimated_cost_usd is not None
            and self.estimated_cost_usd is not None
            and self.estimated_cost_usd >= self.max_estimated_cost_usd
        ):
            return "estimated_cost_usd"
        return None

    @property
    def fraction_used(self) -> float:
        fractions: list[Decimal] = []
        if self.max_total_tokens:
            fractions.append(Decimal(self.billable_tokens) / Decimal(self.max_total_tokens))
        if self.max_estimated_cost_usd and self.estimated_cost_usd is not None:
            fractions.append(self.estimated_cost_usd / self.max_estimated_cost_usd)
        return float(max(fractions, default=Decimal("0")))

    @property
    def should_warn(self) -> bool:
        if self._warned or self.fraction_used < float(WARN_FRACTION):
            return False
        self._warned = True
        return True

    @property
    def finalization_remaining(self) -> int:
        return max(0, self._finalization_allowance - self._finalization_used)

    def enter_finalization(self, allowance: int = FINALIZATION_REQUESTS) -> bool:
        if self.exhausted_dimension is None or self._finalization_allowance:
            return False
        self._finalization_allowance = max(0, int(allowance))
        return True

    def allow_next_request(self) -> bool:
        if self.exhausted_dimension is None:
            return True
        if self.finalization_remaining <= 0:
            return False
        self._finalization_used += 1
        return True

    @property
    def is_exhausted(self) -> bool:
        return self.exhausted_dimension is not None and self.finalization_remaining <= 0

    @property
    def continuation_exhausted(self) -> bool:
        return self.continuation_count >= MAX_BUDGET_CONTINUATIONS

    def consume(self, snapshot: BudgetSnapshot) -> None:
        self._snapshot = self._snapshot + snapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._snapshot.to_dict(),
            "max_total_tokens": self.max_total_tokens,
            "max_estimated_cost_usd": (
                str(self.max_estimated_cost_usd) if self.max_estimated_cost_usd is not None else None
            ),
            "remaining": self.remaining,
            "remaining_cost_usd": (
                str(self.remaining_cost_usd) if self.remaining_cost_usd is not None else None
            ),
            "fraction_used": self.fraction_used,
            "exhausted_dimension": self.exhausted_dimension,
            "continuation_count": self.continuation_count,
        }


def budget_from_env(existing: Optional[TokenBudget] = None) -> Optional[TokenBudget]:
    """Create a run budget from dispatcher-owned env once; invalid values fail open."""
    if existing is not None:
        return existing
    token_raw = os.environ.get("HERMES_KANBAN_MAX_TOTAL_TOKENS")
    cost_raw = os.environ.get("HERMES_KANBAN_MAX_ESTIMATED_COST_USD")
    if token_raw is None and cost_raw is None:
        return None
    try:
        tokens = int(token_raw) if token_raw is not None else None
        cost = Decimal(cost_raw) if cost_raw is not None else None
        continuation = int(os.environ.get("HERMES_KANBAN_BUDGET_CONTINUATION_COUNT", "0"))
        if (tokens is not None and tokens < 1) or (cost is not None and cost <= 0) or continuation < 0:
            return None
    except (InvalidOperation, TypeError, ValueError):
        return None
    return TokenBudget(
        tokens,
        max_estimated_cost_usd=cost,
        continuation_count=continuation,
    )


RESOURCE_BUDGET_CHECKPOINT_NOTICE = (
    "[SYSTEM NOTICE — resource budget checkpoint] This Kanban run is nearing its "
    "token or estimated-cost ceiling. Finish the current atomic edit or test, record "
    "changed files and verification evidence, and do not start a new workstream."
)


def accrue_usage(
    agent: Any, usage: Any, *, estimated_cost_usd: Any = None,
) -> tuple[bool, bool]:
    """Accrue one provider response and return ``(warned, finalizing)``."""
    budget = getattr(agent, "_token_budget", None)
    if budget is None:
        return False, False
    budget.consume(BudgetSnapshot.from_usage(usage, estimated_cost_usd=estimated_cost_usd))
    warned = budget.should_warn
    if warned:
        agent._token_budget_warning_pending = True
    finalizing = budget.enter_finalization(FINALIZATION_REQUESTS)
    return warned, finalizing


def prepare_budget_request(agent: Any, messages: list[dict]) -> bool:
    """Append the one-shot warning cache-safely, then admit or deny a request."""
    if getattr(agent, "_token_budget_warning_pending", False):
        from agent.context_compressor import _DB_PERSISTED_MARKER

        tail = messages[-1] if messages else None
        if isinstance(tail, dict) and tail.get("role") == "tool" and not tail.get(_DB_PERSISTED_MARKER):
            content = tail.get("content", "")
            if isinstance(content, str):
                tail["content"] = content + "\n\n" + RESOURCE_BUDGET_CHECKPOINT_NOTICE
                agent._token_budget_warning_pending = False
            elif isinstance(content, list) or content is None:
                tail["content"] = [
                    *(content or []),
                    {"type": "text", "text": RESOURCE_BUDGET_CHECKPOINT_NOTICE},
                ]
                agent._token_budget_warning_pending = False
    budget = getattr(agent, "_token_budget", None)
    if budget is None or budget.allow_next_request():
        return True
    agent._token_budget_exhausted = True
    return False


def record_budget_yield(agent: Any, messages: list[dict], reason: str) -> bool:
    """Checkpoint and atomically requeue the active Kanban run.

    The dispatcher run id is a compare-and-swap guard: a stale worker cannot
    requeue a card claimed by a newer run.
    """
    task_id = os.getenv("HERMES_KANBAN_TASK")
    try:
        run_id = int(os.getenv("HERMES_KANBAN_RUN_ID", ""))
    except ValueError:
        run_id = 0
    budget = getattr(agent, "_token_budget", None)
    if not task_id or run_id <= 0 or budget is None:
        return False

    workspace = os.getenv("HERMES_KANBAN_WORKSPACE", "")
    checkpoint = build_workspace_checkpoint(workspace, messages)
    checkpoint["reason"] = reason
    checkpoint["budget"] = budget.to_dict()
    summary = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))

    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    conn = kbc.connect()
    try:
        status = kbd._finalize_budget_yielded(
            conn,
            task_id,
            expected_run_id=run_id,
            handoff_summary=summary,
            budget_snapshot=budget.to_dict(),
            checkpoint=checkpoint,
        )
        if status is None:
            return False
        agent._kanban_lifecycle_called = True
        return True
    finally:
        conn.close()


def build_workspace_checkpoint(workspace: str, messages: list[dict]) -> dict[str, Any]:
    """Capture a content-free git progress marker and recent test receipt."""
    checkpoint: dict[str, Any] = {
        "progress_marker": None,
        "commit": None,
        "changed_files": [],
        "test_evidence": None,
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=workspace,
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout
        changed_files = []
        for line in status.splitlines():
            path = line[3:] if len(line) > 3 else ""
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            if path:
                changed_files.append(path)
        checkpoint.update(
            progress_marker=hashlib.sha256((commit + "\0" + status).encode()).hexdigest(),
            commit=commit,
            changed_files=changed_files[:100],
        )
    except (OSError, subprocess.SubprocessError):
        pass
    receipt = re.compile(r"\b\d+ passed(?:[^\n]*)", re.IGNORECASE)
    for message in reversed(messages[-100:]):
        match = receipt.search(str(message.get("content", "")))
        if match:
            checkpoint["test_evidence"] = match.group(0)[:200]
            break
    return checkpoint
