"""Behavior contracts for per-card Kanban token and cost budgets."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc._INITIALIZED_PATHS.clear()
    kbc.init_db()
    return home


def test_budget_snapshot_excludes_cache_reads_from_billable_tokens():
    from agent.kanban_budget import BudgetSnapshot

    snapshot = BudgetSnapshot(
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=10_000,
        cache_write_tokens=30,
        estimated_cost_usd=Decimal("0.25"),
    )

    assert snapshot.total_tokens == 10_150
    assert snapshot.billable_tokens == 150
    assert snapshot.estimated_cost_usd == Decimal("0.25")


def test_budget_warns_once_and_allows_two_finalization_requests():
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    budget = TokenBudget(max_total_tokens=100)
    budget.consume(BudgetSnapshot(input_tokens=75))
    assert budget.should_warn is True
    assert budget.should_warn is False

    budget.consume(BudgetSnapshot(output_tokens=25))
    assert budget.enter_finalization(allowance=2) is True
    assert budget.allow_next_request() is True
    assert budget.allow_next_request() is True
    assert budget.allow_next_request() is False
    assert budget.is_exhausted is True


def test_cost_ceiling_uses_decimal_and_triggers_the_same_yield_path():
    from agent.kanban_budget import BudgetSnapshot, TokenBudget

    budget = TokenBudget(max_total_tokens=None, max_estimated_cost_usd=Decimal("0.10"))
    budget.consume(BudgetSnapshot(input_tokens=1, estimated_cost_usd=Decimal("0.075")))
    assert budget.should_warn is True
    budget.consume(BudgetSnapshot(output_tokens=1, estimated_cost_usd=Decimal("0.025")))
    assert budget.enter_finalization(allowance=2) is True
    assert budget.exhausted_dimension == "estimated_cost_usd"


def test_budget_limits_round_trip_and_migrate_additively(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="bounded",
            assignee="worker",
            max_total_tokens=42_000,
            max_estimated_cost_usd=Decimal("3.75"),
        )
        task = kb.get_task(conn, task_id)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}

    assert task is not None
    assert task.max_total_tokens == 42_000
    assert task.max_estimated_cost_usd == Decimal("3.75")
    assert task.budget_continuation_count == 0
    assert {"max_total_tokens", "max_estimated_cost_usd", "budget_continuation_count"} <= columns


def test_cli_create_and_json_surface_round_trip_budget_limits(kanban_home, capsys):
    from hermes_cli import kanban as kanban_cli

    parser = argparse.ArgumentParser()
    kanban_cli.build_parser(parser.add_subparsers())
    args = parser.parse_args([
        "kanban", "create", "bounded CLI task", "--assignee", "worker",
        "--max-total-tokens", "1234", "--max-estimated-cost-usd", "1.25", "--json",
    ])

    assert kanban_cli.kanban_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["max_total_tokens"] == 1234
    assert payload["max_estimated_cost_usd"] == "1.25"


def _claim(conn, task_id):
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    return claimed.current_run_id


def test_budget_yield_requeues_twice_then_blocks_without_counting_failure(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="large task", assignee="worker", max_total_tokens=100
        )

        for continuation in (1, 2):
            run_id = _claim(conn, task_id)
            status = kbd._finalize_budget_yielded(
                conn,
                task_id,
                expected_run_id=run_id,
                handoff_summary=f"checkpoint {continuation}",
                budget_snapshot={"billable_tokens": 100, "max_total_tokens": 100},
                checkpoint={"progress_marker": f"marker-{continuation}"},
            )
            assert status == "ready"

        run_id = _claim(conn, task_id)
        status = kbd._finalize_budget_yielded(
            conn,
            task_id,
            expected_run_id=run_id,
            handoff_summary="checkpoint 3",
            budget_snapshot={"billable_tokens": 100, "max_total_tokens": 100},
            checkpoint={"progress_marker": "marker-3"},
        )
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert status == "blocked"
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.budget_continuation_count == 3
    assert task.consecutive_failures == 0
    assert [run.outcome for run in runs] == ["budget_yielded"] * 3
    assert runs[-1].metadata["billable_tokens"] == 100


def test_budget_yield_receipt_is_compare_and_swap_idempotent(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="yield once", assignee="worker", max_total_tokens=10)
        run_id = _claim(conn, task_id)
        first = kbd._finalize_budget_yielded(
            conn,
            task_id,
            expected_run_id=run_id,
            handoff_summary="handoff",
            budget_snapshot={"billable_tokens": 10, "max_total_tokens": 10},
            checkpoint={"progress_marker": "one"},
        )
        duplicate = kbd._finalize_budget_yielded(
            conn,
            task_id,
            expected_run_id=run_id,
            handoff_summary="duplicate",
            budget_snapshot={"billable_tokens": 20, "max_total_tokens": 10},
            checkpoint={"progress_marker": "two"},
        )
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert first == "ready"
    assert duplicate is None
    assert task is not None and task.budget_continuation_count == 1
    assert len(runs) == 1


def test_unchanged_budget_checkpoint_blocks_no_progress(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="stalled", assignee="worker", max_total_tokens=10)
        for expected_status in ("ready", "blocked"):
            run_id = _claim(conn, task_id)
            status = kbd._finalize_budget_yielded(
                conn,
                task_id,
                expected_run_id=run_id,
                handoff_summary="same state",
                budget_snapshot={"billable_tokens": 10, "max_total_tokens": 10},
                checkpoint={"progress_marker": "same"},
            )
            assert status == expected_status
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert task is not None and task.block_kind == "needs_input"
    blocked = [event for event in events if event.kind == "budget_yielded_blocked"]
    assert blocked[-1].payload["reason"] == "no_progress"


def test_yielded_continuations_dispatch_behind_fresh_work(kanban_home):
    with kbc.connect() as conn:
        yielded = kb.create_task(conn, title="yielded", assignee="worker", priority=99)
        fresh = kb.create_task(conn, title="fresh", assignee="worker", priority=1)
        conn.execute(
            "UPDATE tasks SET budget_continuation_count = 1 WHERE id = ?", (yielded,)
        )
        conn.commit()

        rows = kbd._lane_rows(conn, "ready")

    assert [row["id"] for row in rows[:2]] == [fresh, yielded]


def test_runtime_usage_starts_finalization_and_warning_is_cache_safe():
    from agent.kanban_budget import TokenBudget, accrue_usage, prepare_budget_request
    from agent.usage_pricing import CanonicalUsage

    agent = SimpleNamespace(
        _token_budget=TokenBudget(max_total_tokens=100),
        _token_budget_warning_pending=False,
        _token_budget_exhausted=False,
    )
    usage = CanonicalUsage(
        input_tokens=70,
        output_tokens=10,
        cache_read_tokens=50_000,
        cache_write_tokens=20,
        request_count=1,
    )

    warned, finalizing = accrue_usage(agent, usage, estimated_cost_usd=Decimal("0.01"))
    messages = [{"role": "tool", "content": "test result"}]
    assert prepare_budget_request(agent, messages) is True

    assert warned is True
    assert finalizing is True
    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert "resource budget checkpoint" in messages[0]["content"]
    assert prepare_budget_request(agent, messages) is True
    assert prepare_budget_request(agent, messages) is False
    assert agent._token_budget_exhausted is True


def test_budget_from_env_preserves_one_budget_across_turns(monkeypatch):
    from agent.kanban_budget import TokenBudget, budget_from_env

    monkeypatch.setenv("HERMES_KANBAN_MAX_TOTAL_TOKENS", "1000")
    monkeypatch.setenv("HERMES_KANBAN_MAX_ESTIMATED_COST_USD", "0.25")
    monkeypatch.setenv("HERMES_KANBAN_BUDGET_CONTINUATION_COUNT", "2")

    first = budget_from_env()
    second = budget_from_env(first)

    assert isinstance(first, TokenBudget)
    assert second is first
    assert first.max_total_tokens == 1000
    assert first.max_estimated_cost_usd == Decimal("0.25")
    assert first.continuation_count == 2


def test_budget_yield_checkpoints_and_requeues_with_run_cas(kanban_home, monkeypatch, tmp_path):
    from agent.kanban_budget import BudgetSnapshot, TokenBudget, record_budget_yield

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="yield me",
            assignee="worker",
            max_total_tokens=100,
        )
        claimed = kb.claim_task(conn, task_id, claimer="test-worker")
        assert claimed is not None
        task = kb.get_task(conn, task_id)
        assert task is not None
        run_id = task.current_run_id
        assert run_id is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    budget = TokenBudget(max_total_tokens=100)
    budget.consume(BudgetSnapshot(input_tokens=100))
    agent = SimpleNamespace(_token_budget=budget, _kanban_lifecycle_called=False)

    assert record_budget_yield(
        agent,
        [{"role": "assistant", "content": "checkpoint note"}],
        "resource_budget_yielded",
    ) is True

    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert task is not None
    assert run is not None
    assert task.status == "ready"
    assert task.current_run_id is None
    assert task.budget_continuation_count == 1
    assert run["status"] == "released"
    assert run["outcome"] == "budget_yielded"
    assert any(event.kind == "budget_yielded" for event in events)
    assert agent._kanban_lifecycle_called is True

    # A stale duplicate finalizer cannot increment or requeue a newer claim.
    assert record_budget_yield(agent, [], "resource_budget_yielded") is False
    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.budget_continuation_count == 1
