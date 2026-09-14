"""Regression coverage for local Kanban iteration and run controls."""
from __future__ import annotations

import logging
from pathlib import Path

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


def test_card_iteration_budget_round_trips_and_reaches_worker_argv(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="large task",
            assignee="worker",
            max_iterations=80,
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.max_iterations == 80
    command = kbd._worker_argv(task, "worker", None)
    index = command.index("--max-turns")
    assert command[index + 1] == "80"
    assert index < command.index("chat")


def test_iteration_exhaustion_is_sticky_and_compare_and_swap_guarded(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="too large", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        applied = kbd._record_task_failure(
            conn,
            task_id,
            error=(
                "Iteration budget exhausted (30/30) — task too large for its budget; "
                "resize max_iterations or split the task, then unblock"
            ),
            outcome="iteration_budget_exhausted",
            force_trip=True,
            sticky_block_kind="needs_input",
            run_summary="partial work is preserved",
            release_claim=True,
            end_run=True,
            expected_run_id=run_id,
            event_payload_extra={"block_cause": "iteration_budget_exhausted"},
        )
        duplicate = kbd._record_task_failure(
            conn,
            task_id,
            error="duplicate",
            outcome="iteration_budget_exhausted",
            force_trip=True,
            sticky_block_kind="needs_input",
            release_claim=True,
            end_run=True,
            expected_run_id=run_id,
        )
        task = kb.get_task(conn, task_id)
        run = kb.list_runs(conn, task_id)[0]
        events = kb.list_events(conn, task_id)

    assert applied is True
    assert duplicate is False
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.current_run_id is None
    assert task.consecutive_failures == 1
    assert run.outcome == "gave_up"
    assert run.summary == "partial work is preserved"
    assert len([event for event in events if event.kind == "gave_up"]) == 1
    assert len([event for event in events if event.kind == "blocked"]) == 1


def test_worker_pid_update_rejects_a_stale_run_receipt(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="claimed task", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None

        applied = kbd._set_worker_pid(
            conn,
            task_id,
            43210,
            expected_run_id=claimed.current_run_id + 1,
        )
        task = kb.get_task(conn, task_id)
        run = kb.list_runs(conn, task_id)[0]
        events = kb.list_events(conn, task_id)

    assert applied is False
    assert task is not None
    assert task.worker_pid is None
    assert run.worker_pid is None
    assert not any(event.kind == "spawned" for event in events)


def test_claim_task_rejects_an_unknown_source_status(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="unclaimed task", assignee="worker")
        with pytest.raises(ValueError, match="from_status"):
            kb.claim_task(conn, task_id, from_status="ready' OR 1=1 --")


def test_iteration_finalizer_forwards_worker_run_receipt(kanban_home, monkeypatch):
    from agent.turn_finalizer import _record_kanban_budget_exhausted

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "37")
    captured = {}

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr("hermes_cli.kanban_db_connect.connect", _Conn)
    monkeypatch.setattr(
        "hermes_cli.kanban_db_dispatch._record_task_failure",
        lambda _conn, task_id, **kwargs: captured.update(task_id=task_id, **kwargs),
    )

    _record_kanban_budget_exhausted(
        "t_receipt",
        api_call_count=30,
        max_iterations=30,
        logger=logging.getLogger("test"),
        handoff_summary="verified partial handoff",
    )

    assert captured["expected_run_id"] == 37
    assert captured["outcome"] == "iteration_budget_exhausted"
    assert captured["force_trip"] is True
    assert captured["sticky_block_kind"] == "needs_input"
    assert captured["run_summary"] == "verified partial handoff"
    assert captured["event_payload_extra"]["block_cause"] == "iteration_budget_exhausted"
