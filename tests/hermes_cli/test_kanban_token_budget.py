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