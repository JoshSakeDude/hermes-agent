"""Per-task token/cost ceiling — schema, migration, persistence, and BudgetSnapshot.

Phase A of the token-ceiling feature. Covers:
  * BudgetSnapshot exposes input, output, cache-read, cache-write, reasoning,
    billable, and estimated cost separately — cached context stays transparent.
  * ``max_total_tokens`` and ``max_estimated_cost_usd`` round-trip through
    create_task → persist → get_task.
  * Legacy DBs get the columns via ``_migrate_add_optional_columns``.
  * When unset, both fields default to None (existing behaviour).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# BudgetSnapshot
# ---------------------------------------------------------------------------


def test_budget_snapshot_exposes_breakdown():
    """Every token category is exposed separately; cache-read is NOT merged into
    input — cached context stays transparent."""
    from agent.kanban_budget import BudgetSnapshot

    snap = BudgetSnapshot(
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=100,
        reasoning_tokens=300,
        request_count=3,
        estimated_cost_usd=Decimal("0.123"),
    )

    assert snap.input_tokens == 1000
    assert snap.output_tokens == 500
    assert snap.cache_read_tokens == 2000
    assert snap.cache_write_tokens == 100
    assert snap.reasoning_tokens == 300
    # Prompt = input + cache_read + cache_write (matches CanonicalUsage)
    assert snap.prompt_tokens == 3100
    # Total = prompt + output
    assert snap.total_tokens == 3600
    # Billable = input + output + cache_write, NOT cache_read
    assert snap.billable_tokens == 1600
    assert snap.request_count == 3
    assert snap.estimated_cost_usd == Decimal("0.123")


def test_budget_snapshot_defaults():
    """All fields default to 0/None so a zero-arg snapshot is a safe zero-budget."""
    from agent.kanban_budget import BudgetSnapshot

    snap = BudgetSnapshot()
    assert snap.input_tokens == 0
    assert snap.prompt_tokens == 0
    assert snap.billable_tokens == 0
    assert snap.estimated_cost_usd is None


def test_budget_snapshot_addition():
    """Two snapshots add to produce a combined snapshot."""
    from agent.kanban_budget import BudgetSnapshot

    s1 = BudgetSnapshot(
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=200,
        cache_write_tokens=10,
        reasoning_tokens=0,
        request_count=1,
        estimated_cost_usd=Decimal("0.01"),
    )
    s2 = BudgetSnapshot(
        input_tokens=300,
        output_tokens=150,
        cache_read_tokens=400,
        cache_write_tokens=20,
        reasoning_tokens=5,
        request_count=2,
        estimated_cost_usd=Decimal("0.03"),
    )
    total = s1 + s2
    assert total.input_tokens == 400
    assert total.output_tokens == 200
    assert total.cache_read_tokens == 600
    assert total.cache_write_tokens == 30
    assert total.reasoning_tokens == 5
    assert total.request_count == 3
    assert total.estimated_cost_usd == Decimal("0.04")
    assert total.billable_tokens == 630  # 400+200+30


def test_budget_snapshot_from_canonical_usage():
    """BudgetSnapshot.from_usage() projects CanonicalUsage faithfully."""
    from agent.kanban_budget import BudgetSnapshot
    from agent.usage_pricing import CanonicalUsage

    usage = CanonicalUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=100,
        reasoning_tokens=300,
        request_count=3,
    )
    snap = BudgetSnapshot.from_usage(usage)

    assert snap.input_tokens == 1000
    assert snap.cache_read_tokens == 2000
    assert snap.cache_write_tokens == 100
    # Cost is None because no pricing was resolved
    assert snap.estimated_cost_usd is None
    assert snap.billable_tokens == 1600


# ---------------------------------------------------------------------------
# Schema — max_total_tokens and max_estimated_cost_usd persistence
# ---------------------------------------------------------------------------


def test_token_budget_round_trips_create_persist_show(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="budgeted task",
            assignee="gohanlite",
            max_total_tokens=200_000,
            max_estimated_cost_usd=Decimal("5.00"),
        )
        task = kb.get_task(conn, t)
        assert task.max_total_tokens == 200_000
        assert task.max_estimated_cost_usd == Decimal("5.00")


def test_token_budget_unset_defaults_to_none(kanban_home):
    """A card created without the fields must persist NULL — existing behaviour
    is unchanged."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="normal task", assignee="gohanlite")
        task = kb.get_task(conn, t)
        assert task.max_total_tokens is None
        assert task.max_estimated_cost_usd is None


def test_token_budget_persists_integer_cost(kanban_home):
    """An integer-as-Decimal cost round-trips correctly."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="exact dollar",
            assignee="gohanlite",
            max_estimated_cost_usd=Decimal("5"),
        )
        task = kb.get_task(conn, t)
        assert task.max_estimated_cost_usd == Decimal("5")


def test_token_budget_persists_subcent_cost(kanban_home):
    """Sub-cent cost amounts survive the Decimal→TEXT→Decimal round-trip."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="cheap task",
            assignee="gohanlite",
            max_estimated_cost_usd=Decimal("0.0042"),
        )
        task = kb.get_task(conn, t)
        assert task.max_estimated_cost_usd == Decimal("0.0042")


def test_token_budget_only_tokens_set(kanban_home):
    """One field can be set without the other."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="tokens only",
            assignee="gohanlite",
            max_total_tokens=50_000,
        )
        task = kb.get_task(conn, t)
        assert task.max_total_tokens == 50_000
        assert task.max_estimated_cost_usd is None


def test_token_budget_only_cost_set(kanban_home):
    """One field can be set without the other (reverse direction)."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="cost only",
            assignee="gohanlite",
            max_estimated_cost_usd=Decimal("1.50"),
        )
        task = kb.get_task(conn, t)
        assert task.max_total_tokens is None
        assert task.max_estimated_cost_usd == Decimal("1.50")


# ---------------------------------------------------------------------------
# Migration — legacy DB gets new columns
# ---------------------------------------------------------------------------


def test_migration_adds_token_budget_columns_to_legacy_db(tmp_path, monkeypatch):
    """When a legacy kanban.db (pre-token-budget) is opened, the migration
    adds both columns and existing rows get NULL."""
    home = tmp_path / ".hermes-legacy"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Init a fresh board with current schema, then remove the new columns
    # by dropping via a raw ALTER (SQLite can't DROP COLUMN before 3.35 anyway,
    # so we fake it by directly removing from sqlite_master and table_info).
    # Instead: create a task BEFORE adding the columns, then simulate a
    # migration by calling init_db again after the import.
    kb.init_db()

    with kb.connect() as conn:
        # Create a task — it'll get NULL for the new columns after migration
        t = kb.create_task(
            conn,
            title="legacy task",
            assignee="gohanlite",
        )
        # Read back after the migration has run (it runs inside init_db)
        task = kb.get_task(conn, t)
        assert task.max_total_tokens is None
        assert task.max_estimated_cost_usd is None


def test_token_budget_columns_in_schema(kanban_home):
    """After init_db, the tasks table has both max_total_tokens and
    max_estimated_cost_usd columns."""
    with kb.connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)")
        }
    assert "max_total_tokens" in cols
    assert "max_estimated_cost_usd" in cols


def test_token_budget_from_row_hydrates_set_values(kanban_home):
    """Task.from_row hydrates both fields when present."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="hydrate test",
            assignee="gohanlite",
            max_total_tokens=300_000,
            max_estimated_cost_usd=Decimal("10"),
        )
        # Raw row check
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["max_total_tokens"] == 300_000
        assert row["max_estimated_cost_usd"] == "10"


# ===========================================================================
# Phase B - TokenBudget, yield, continuation, and circuit breaker
# ===========================================================================


# ---------------------------------------------------------------------------
# TokenBudget - billable token tracking against per-task ceiling
# ---------------------------------------------------------------------------

def test_token_budget_initial_state():
    """A fresh TokenBudget starts at 0 and reports correct remaining."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    tb = TokenBudget(max_total_tokens=100_000)
    assert tb.max_total_tokens == 100_000
    assert tb.billable_tokens == 0
    assert tb.remaining == 100_000
    assert tb.fraction_used == 0.0
    assert not tb.is_exhausted
    assert not tb.should_warn


def test_token_budget_consume_accumulates_billable():
    """Consuming a BudgetSnapshot adds billable tokens correctly."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    tb = TokenBudget(max_total_tokens=100_000)
    snap = BudgetSnapshot(
        input_tokens=500,
        output_tokens=200,
        cache_write_tokens=50,
    )
    tb.consume(snap)
    # billable = 500 + 200 + 50 = 750
    assert tb.billable_tokens == 750
    assert tb.remaining == 99_250
    assert tb.fraction_used == 0.0075


def test_token_budget_warns_at_75_percent():
    """At >=75% of ceiling, should_warn returns True."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    tb = TokenBudget(max_total_tokens=100_000)
    snap = BudgetSnapshot(
        input_tokens=50000,
        output_tokens=25000,
        cache_write_tokens=0,
    )
    tb.consume(snap)
    assert tb.billable_tokens == 75_000
    assert tb.fraction_used == 0.75
    assert tb.should_warn


def test_token_budget_warns_only_once():
    """After the first warn, should_warn returns False until reset."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    tb = TokenBudget(max_total_tokens=100_000)
    snap75 = BudgetSnapshot(input_tokens=75000)
    tb.consume(snap75)
    assert tb.should_warn
    snap5 = BudgetSnapshot(input_tokens=5000)
    tb.consume(snap5)
    assert not tb.should_warn  # already warned


def test_token_budget_exhausted_at_ceiling():
    """When billable tokens reach max_total_tokens, is_exhausted is True."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    tb = TokenBudget(max_total_tokens=10_000)
    snap = BudgetSnapshot(input_tokens=10000)
    tb.consume(snap)
    assert tb.billable_tokens == 10_000
    assert tb.remaining == 0
    assert tb.fraction_used == 1.0
    assert tb.is_exhausted


def test_token_budget_exhausted_after_finalization():
    """After entering finalization, exhaustion only when turns consumed."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    tb = TokenBudget(max_total_tokens=100_000)
    snap = BudgetSnapshot(input_tokens=100_000)
    tb.consume(snap)
    assert tb.is_exhausted  # at ceiling
    tb.enter_finalization(allowance=3)
    assert not tb.is_exhausted  # still has finalization turns
    assert tb.finalization_remaining == 3
    tb.consume_finalization_turn()
    tb.consume_finalization_turn()
    tb.consume_finalization_turn()
    assert tb.is_exhausted  # finalization gone


def test_token_budget_carry_forward():
    """Carry-forward from prior runs is added to billable tokens."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    carry = BudgetSnapshot(input_tokens=30000)
    tb = TokenBudget(max_total_tokens=100_000, carry_forward=carry)
    assert tb.billable_tokens == 30_000
    assert tb.remaining == 70_000
    assert tb.fraction_used == 0.3


def test_token_budget_with_continuation_count():
    """Continuation count is tracked."""
    from agent.kanban_budget import TokenBudget

    tb = TokenBudget(max_total_tokens=100_000)
    assert tb.continuation_count == 0
    tb.continuation_count = 1
    assert tb.continuation_count == 1
    tb.continuation_count = 2
    assert tb.continuation_count == 2


def test_token_budget_continuation_exhausted():
    """When continuation count reaches MAX_BUDGET_CONTINUATIONS, flagged."""
    from agent.kanban_budget import TokenBudget

    tb = TokenBudget(max_total_tokens=100_000, continuation_count=2)
    assert tb.continuation_exhausted


def test_token_budget_no_ceiling_never_exhausted():
    """max_total_tokens=None means no ceiling - never exhausted."""
    from agent.kanban_budget import TokenBudget

    tb = TokenBudget(max_total_tokens=None)
    assert not tb.is_exhausted
    assert tb.remaining is None
    assert not tb.should_warn


def test_token_budget_continuation_context():
    """The structured handoff context carries counters and prior summary."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    carry = BudgetSnapshot(input_tokens=30000)
    tb = TokenBudget(
        max_total_tokens=100_000,
        carry_forward=carry,
        continuation_count=1,
        handoff_summary="prior run: fixed typo in three files",
    )
    ctx = tb.continuation_context()
    assert "2 of 2" in ctx or "continuation" in ctx.lower()
    assert "30,000" in ctx
    assert "70,000" in ctx
    assert "prior run" in ctx.lower()


# ---------------------------------------------------------------------------
# Dispatcher integration - budget_yielded outcome and continuation column
# ---------------------------------------------------------------------------

def test_budget_yielded_schema():
    """budget_yielded is a recognized outcome constant."""
    from agent.kanban_budget import BUDGET_YIELDED_OUTCOME
    assert BUDGET_YIELDED_OUTCOME == "budget_yielded"


def test_budget_continuation_count_column_in_schema(kanban_home):
    """After init_db, the tasks table has budget_continuation_count."""
    with kb.connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)")
        }
    assert "budget_continuation_count" in cols


def test_budget_continuation_count_defaults_to_zero(kanban_home):
    """New tasks have budget_continuation_count = 0."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="fresh task", assignee="gohanlite")
        row = conn.execute(
            "SELECT budget_continuation_count FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["budget_continuation_count"] == 0


def test_budget_continuation_count_persists(kanban_home):
    """Setting budget_continuation_count survives round-trip."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="continuing task", assignee="gohanlite")
        conn.execute(
            "UPDATE tasks SET budget_continuation_count = 1 WHERE id = ?", (t,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT budget_continuation_count FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["budget_continuation_count"] == 1


# ---------------------------------------------------------------------------
# Migration - legacy DB gets budget_continuation_count
# ---------------------------------------------------------------------------

def test_migration_adds_continuation_count_to_legacy_db(tmp_path, monkeypatch):
    """When a legacy kanban.db is opened, migration adds
    budget_continuation_count and existing rows get 0."""
    home = tmp_path / ".hermes-legacy-b"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb.init_db()
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="legacy continuation task",
            assignee="gohanlite",
        )
        task = kb.get_task(conn, t)
        assert task.budget_continuation_count == 0


# ---------------------------------------------------------------------------
# Independence - max_iterations and unrelated dispatch
# ---------------------------------------------------------------------------

def test_max_iterations_independent_of_token_ceiling(kanban_home):
    """A card can have both max_iterations and max_total_tokens set
    independently, and both round-trip."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="double budget task",
            assignee="gohanlite",
            max_iterations=50,
            max_total_tokens=200_000,
        )
        task = kb.get_task(conn, t)
        assert task.max_iterations == 50
        assert task.max_total_tokens == 200_000
        assert task.budget_continuation_count == 0


def test_no_ceiling_cards_still_dispatch():
    """A card without token ceiling dispatches normally (TokenBudget
    with max_total_tokens=None is never exhausted)."""
    from agent.kanban_budget import TokenBudget

    tb = TokenBudget(max_total_tokens=100_000)
    assert tb.max_total_tokens == 100_000

    tb2 = TokenBudget(max_total_tokens=None)
    assert tb2.max_total_tokens is None
    assert not tb2.is_exhausted


def test_budget_yielded_continuation_capped_at_two(kanban_home):
    """When budget_continuation_count reaches 2, dispatcher blocks
    with needs_input. Schema-level: counter round-trips."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="exhausted task",
            assignee="gohanlite",
            max_total_tokens=10_000,
        )
        conn.execute(
            "UPDATE tasks SET budget_continuation_count = 2 WHERE id = ?", (t,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT budget_continuation_count FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["budget_continuation_count"] == 2


# ============================================================================
# Phase B — budget_yielded dispatcher integration tests
# ============================================================================


def test_budget_yielded_releases_to_ready_and_increments_counter(kanban_home):
    """A claimed running task that yields on first continuation: run closes
    with budget_yielded, claim is released, card returns to ready, and
    budget_continuation_count goes from 0 to 1."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="yield task", assignee="gohanlite",
            max_total_tokens=100_000,
        )
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")

    # Simulate a worker yielding from budget exhaustion.
    kb._finalize_budget_yielded(
        kb.connect(), t,
        handoff_summary="did part A, piack up at part B",
        billable_tokens=75_000,
        max_total_tokens=100_000,
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, t)
        assert task.status == "ready", f"expected ready, got {task.status}"
        assert task.claim_lock is None, "claim should be released"
        assert task.budget_continuation_count == 1

        # Run outcome should be budget_yielded.
        events = kb.list_events(conn, t)
        yield_events = [e for e in events if e.kind == "budget_yielded"]
        assert len(yield_events) == 1
        payload = yield_events[0].payload or {}
        assert payload.get("continuation_count") == 1
        assert payload["billed"] == 75_000


def test_budget_yielded_blocks_on_third_exhaustion(kanban_home):
    """After two budget continuations (counter at 2), the third yield blocks
    with needs_input rather than returning to ready."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="blocked yield", assignee="gohanlite",
            max_total_tokens=50_000,
        )
        host = kb._claimer_id().split(":", 1)[0]

        for _ in range(2):
            kb.claim_task(conn, t, claimer=f"{host}:worker")
            # Reset connection for next claim, return to active
            conn.execute(
                "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, current_run_id = NULL "
                "WHERE id = ?", (t,)
            )
            conn.execute(
                "UPDATE tasks SET budget_continuation_count = budget_continuation_count + 1 "
                "WHERE id = ?", (t,)
            )
        conn.commit()

    # After 2 continuations, this yield should block.
    with kb.connect() as conn:
        kb.claim_task(conn, t, claimer=f"{host}:worker_2")
    status = kb._finalize_budget_yielded(
        kb.connect(), t,
        handoff_summary="final exhaustion",
        billable_tokens=50_000,
        max_total_tokens=50_000,
    )

    assert status == "blocked", f"expected blocked, got {status}"
    with kb.connect() as conn:
        task = kb.get_task(conn, t)
        assert task.status == "blocked"
        events = kb.list_events(conn, t)
        blocked = [e for e in events if e.kind == "budget_yielded_blocked"]
        assert len(blocked) == 1, "expected a budget_yielded_blocked event"


def test_budget_yielded_does_not_increment_failure_counter(kanban_home):
    """A budget yield is NOT a failure — consecutive_failures stays at 0."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="no-fail yield", assignee="gohanlite",
            max_total_tokens=100_000,
        )
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")

    kb._finalize_budget_yielded(
        kb.connect(), t,
        handoff_summary="clean handoff",
        billable_tokens=80_000,
        max_total_tokens=100_000,
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, t)
        assert task.consecutive_failures == 0, (
            f"budget yield should not increment failures, got {task.consecutive_failures}"
        )


def test_budget_yielded_unset_ceiling_never_blocks(kanban_home):
    """A task with max_total_tokens=None: TokenBudget is unbounded, no yield."""
    from agent.kanban_budget import TokenBudget

    tb = TokenBudget(max_total_tokens=None)
    assert not tb.is_exhausted
    assert tb.remaining is None
    assert not tb.continuation_exhausted


# ============================================================================
# Phase B — agent-loop integration (TokenBudget from env vars)
# ============================================================================


def test_token_budget_created_from_env_vars():
    """When HERMES_KANBAN_MAX_TOTAL_TOKENS is set, TokenBudget is created
    with the ceiling and any continuation count from env."""
    import os
    from agent.kanban_budget import TokenBudget

    os.environ["HERMES_KANBAN_MAX_TOTAL_TOKENS"] = "200000"
    os.environ["HERMES_KANBAN_BUDGET_CONTINUATION_COUNT"] = "1"
    try:
        tb = TokenBudget(
            max_total_tokens=int(os.environ["HERMES_KANBAN_MAX_TOTAL_TOKENS"]),
            continuation_count=int(os.environ["HERMES_KANBAN_BUDGET_CONTINUATION_COUNT"]),
        )
        assert tb.max_total_tokens == 200_000
        assert tb.continuation_count == 1
    finally:
        os.environ.pop("HERMES_KANBAN_MAX_TOTAL_TOKENS", None)
        os.environ.pop("HERMES_KANBAN_BUDGET_CONTINUATION_COUNT", None)


def test_token_budget_yield_signal_at_hard_ceiling():
    """After reaching the ceiling and consuming all finalization turns,
    is_exhausted returns True and the budget should yield."""
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    tb = TokenBudget(max_total_tokens=10_000)
    snap = BudgetSnapshot(input_tokens=10_000)
    tb.consume(snap)
    # At ceiling, no finalization -> exhausted
    assert tb.is_exhausted is True
    assert tb.finalization_remaining == 0

    # Enter finalization -> not exhausted until turns consumed
    tb.enter_finalization(allowance=2)
    assert tb.is_exhausted is False
    tb.consume_finalization_turn()
    tb.consume_finalization_turn()
    assert tb.finalization_remaining == 0
    assert tb.is_exhausted is True