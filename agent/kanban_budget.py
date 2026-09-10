"""Per-run token budget snapshot and ceiling definitions.

Phase A of the token-ceiling feature - storage and accounting only.
Dispatchers and agent-loop yield checks live in later phases.
Phase B adds TokenBudget, yield, continuation, and circuit breaker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import re
import subprocess
from typing import Optional


@dataclass(frozen=True)
class BudgetSnapshot:
    """A point-in-time snapshot of token usage across categories.

    Designed so cached context stays transparent: ``cache_read_tokens`` is
    exposed separately from ``input_tokens``, never silently merged. The
    ``billable_tokens`` property sums what the provider actually charges
    (input + output + cache_write, excluding cache_read).

    Properties match the semantics of ``CanonicalUsage`` so the two can
    be compared directly when projecting against a ceiling.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    estimated_cost_usd: Optional[Decimal] = None

    @property
    def prompt_tokens(self) -> int:
        """input + cache_read + cache_write (matches CanonicalUsage.prompt_tokens)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        """prompt_tokens + output_tokens (matches CanonicalUsage.total_tokens)."""
        return self.prompt_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """Tokens the provider charges for: input + output + cache_write.

        Excludes cache_read (heavily discounted) and reasoning_tokens
        (subsumed into output for billing in most models).
        """
        return self.input_tokens + self.output_tokens + self.cache_write_tokens

    def __add__(self, other: BudgetSnapshot) -> BudgetSnapshot:
        """Combine two snapshots into one across-sessions sum."""
        if not isinstance(other, BudgetSnapshot):
            return NotImplemented
        cost = None
        if self.estimated_cost_usd is not None and other.estimated_cost_usd is not None:
            cost = self.estimated_cost_usd + other.estimated_cost_usd
        elif self.estimated_cost_usd is not None:
            cost = self.estimated_cost_usd
        elif other.estimated_cost_usd is not None:
            cost = other.estimated_cost_usd
        return BudgetSnapshot(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            request_count=self.request_count + other.request_count,
            estimated_cost_usd=cost,
        )

    @classmethod
    def from_usage(cls, usage, *, estimated_cost_usd=None) -> BudgetSnapshot:
        """Project a ``CanonicalUsage`` into a BudgetSnapshot.

        ``estimated_cost_usd`` is optional - pass a ``CostResult.amount_usd``
        when available, otherwise the snapshot carries None.
        """
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            request_count=usage.request_count,
            estimated_cost_usd=estimated_cost_usd,
        )


# ============================================================================
# Phase B - TokenBudget, yield, continuation, circuit breaker
# ============================================================================

MAX_BUDGET_CONTINUATIONS = 2

BUDGET_YIELDED_OUTCOME = "budget_yielded"

WARN_FRACTION = 0.75


def build_workspace_checkpoint(workspace: str, messages: list[dict]) -> dict:
    """Capture a content-free git progress marker and recent test evidence."""
    checkpoint = {
        "progress_marker": None,
        "commit": None,
        "changed_files": [],
        "test_evidence": None,
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace,
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace, check=True, capture_output=True, text=True, timeout=5,
        ).stdout
        changed_files = []
        for line in status.splitlines():
            path = line[3:] if len(line) > 3 else ""
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            if path:
                changed_files.append(path)
        checkpoint.update(
            progress_marker=hashlib.sha256(
                (commit + "\0" + status).encode("utf-8", errors="replace")
            ).hexdigest(),
            commit=commit,
            changed_files=changed_files[:100],
        )
    except (OSError, subprocess.SubprocessError):
        pass

    passed_pattern = re.compile(r"\b\d+ passed(?:[^\n]*)", re.IGNORECASE)
    for message in reversed(messages[-100:]):
        match = passed_pattern.search(str(message.get("content", "")))
        if match:
            checkpoint["test_evidence"] = match.group(0)[:200]
            break
    return checkpoint


class TokenBudget:
    """Tracks billable token consumption against a per-task ceiling.

    Works alongside ``IterationBudget`` which tracks turn count.
    Both can be active simultaneously; they are independent.

    When ``max_total_tokens`` is None the budget is unbounded (never
    exhausted, never warns). This is the default for tasks that don't
    set a token ceiling.

    Attributes:
        max_total_tokens: Ceiling in billable tokens, or None.
        carry_forward: Prior-run usage summed from continuation env.
        continuation_count: How many times this task has been continued.
        handoff_summary: Structured summary from the prior run.
    """

    def __init__(
        self,
        max_total_tokens: Optional[int],
        *,
        carry_forward: Optional[BudgetSnapshot] = None,
        continuation_count: int = 0,
        handoff_summary: str = "",
    ):
        self._max_total_tokens = max_total_tokens
        self._snapshot = BudgetSnapshot()
        self._carry_forward = carry_forward or BudgetSnapshot()
        self.continuation_count = continuation_count
        self.handoff_summary = handoff_summary
        self._warned = False
        self._finalization_turns = 0
        self._finalization_used = 0

    @property
    def max_total_tokens(self) -> Optional[int]:
        return self._max_total_tokens

    @property
    def billable_tokens(self) -> int:
        """Billable tokens consumed this run + carry-forward from prior runs."""
        return self._snapshot.billable_tokens + self._carry_forward.billable_tokens

    @property
    def remaining(self) -> Optional[int]:
        """Tokens remaining before the ceiling is hit, or None if unbounded."""
        if self._max_total_tokens is None:
            return None
        return max(0, self._max_total_tokens - self.billable_tokens)

    @property
    def fraction_used(self) -> float:
        """Fraction of the ceiling used (0.0 .. 1.0)."""
        if self._max_total_tokens in (None, 0):
            return 1.0 if self._max_total_tokens == 0 else 0.0
        return self.billable_tokens / self._max_total_tokens

    @property
    def should_warn(self) -> bool:
        """True once when billable usage first crosses the warn threshold."""
        if self._warned:
            return False
        if self._max_total_tokens is None:
            return False
        if self.fraction_used >= WARN_FRACTION:
            self._warned = True
            return True
        return False

    @property
    def is_exhausted(self) -> bool:
        """True when the budget is exhausted (no remaining tokens AND no
        finalization turns left)."""
        if self._max_total_tokens is None:
            return False
        if self.finalization_remaining > 0:
            return False
        return self.remaining == 0

    @property
    def continuation_exhausted(self) -> bool:
        """True when continuations have reached MAX_BUDGET_CONTINUATIONS."""
        return self.continuation_count >= MAX_BUDGET_CONTINUATIONS

    # -- finalization ------------------------------------------------

    @property
    def finalization_remaining(self) -> int:
        """Finalization turns left, 0 if not in finalization."""
        return max(0, self._finalization_turns - self._finalization_used)

    def enter_finalization(self, allowance: int) -> None:
        """Reserve allowance turns for atomic finish/revert + handoff."""
        if self._max_total_tokens is None:
            return
        self._finalization_turns = max(0, allowance)
        self._finalization_used = 0

    def consume_finalization_turn(self) -> None:
        """Consume one finalization turn."""
        if self._finalization_turns > 0:
            self._finalization_used = min(
                self._finalization_turns,
                self._finalization_used + 1,
            )

    def allow_next_request(self) -> bool:
        """Admit a provider request, consuming grace only after the ceiling.

        Once a finite budget has entered finalization, each admitted request
        consumes one grace slot.  The following call returns ``False`` so the
        conversation loop yields before starting another provider request.
        """
        if self._max_total_tokens is None or self.remaining != 0:
            return True
        if self.finalization_remaining <= 0:
            return False
        self.consume_finalization_turn()
        return True

    # -- consumption ------------------------------------------------

    def consume(self, snapshot: BudgetSnapshot) -> None:
        """Accumulate a BudgetSnapshot into this budget."""
        self._snapshot += snapshot

    @property
    def run_snapshot(self) -> BudgetSnapshot:
        """The BudgetSnapshot for THIS run only (excluding carry-forward)."""
        return self._snapshot

    # -- continuation context ---------------------------------------

    def continuation_context(self) -> str:
        """Render a structured handoff string for the next worker.

        Includes continuation count, prior token usage, and any summary
        from the last run. The dispatcher passes this as env var so the
        next worker sees what came before.
        """
        parts = []
        if self._max_total_tokens is not None:
            parts.append(
                "Token budget continued (continuation "
                f"{self.continuation_count + 1}"
                f" of {MAX_BUDGET_CONTINUATIONS})"
            )
            parts.append(f"Maximum: {self._max_total_tokens:,} tokens")
        parts.append(f"Billed so far: {self.billable_tokens:,}")
        if self.remaining is not None:
            parts.append(f"Remaining: {self.remaining:,}")
        if self.handoff_summary:
            parts.append(f"Prior run summary: {self.handoff_summary}")
        return "\n".join(parts)


__all__ = [
    "BudgetSnapshot",
    "TokenBudget",
    "build_workspace_checkpoint",
    "BUDGET_YIELDED_OUTCOME",
    "MAX_BUDGET_CONTINUATIONS",
    "WARN_FRACTION",
]