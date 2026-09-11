"""Narrow deterministic Kanban worker startup failure handling."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("exc", "reason_code", "hint"),
    [
        (
            RuntimeError("argument command: invalid choice: '35' (choose from 'chat')"),
            "worker_cli_invalid_max_turns_35",
            "Check worker launch argument ordering.",
        ),
        (
            FileNotFoundError("hermes"),
            "worker_executable_not_found",
            "Check the Hermes executable path and installation.",
        ),
        (
            ValueError("Profile 'ghost' does not exist"),
            "worker_profile_not_found",
            "Assign an installed Hermes profile.",
        ),
    ],
)
def test_exact_deterministic_startup_allowlist(exc, reason_code, hint):
    assert kb._classify_startup_failure(exc) == {
        "reason_code": reason_code,
        "retryable": False,
        "operator_hint": hint,
    }


def test_unknown_startup_failure_remains_retryable():
    decision = kb._classify_startup_failure(RuntimeError("socket reset"))
    assert decision == {
        "reason_code": "worker_startup_unknown",
        "retryable": True,
        "operator_hint": "Inspect the current worker run log.",
    }


def test_exact_parser_failure_blocks_after_first_attempt_and_persists_decision(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="parser incident", assignee="default")
        kb.recompute_ready(conn, failure_limit=3)
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")

        def fail_spawn(_task, _workspace):
            raise RuntimeError(
                "usage: hermes [-h] {chat}\n"
                "argument command: invalid choice: '35' (choose from 'chat')"
            )

        result = kb.dispatch_once(
            conn,
            spawn_fn=fail_spawn,
            failure_limit=3,
            reconcile_orphans=False,
        )

        task = kb.get_task(conn, task_id)
        assert result.auto_blocked == [task_id]
        assert task.status == "blocked"
        assert task.consecutive_failures == 1
        runs = kb.list_runs(conn, task_id)
        assert len(runs) == 1
        assert runs[0].outcome == "gave_up"
        assert runs[0].metadata["reason_code"] == "worker_cli_invalid_max_turns_35"
        assert runs[0].metadata["retryable"] is False
        assert runs[0].metadata["operator_hint"] == (
            "Check worker launch argument ordering."
        )
        event_kinds = [
            row["kind"] for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchall()
        ]
        assert event_kinds.count("gave_up") == 1
        assert event_kinds.count("blocked") == 1
    finally:
        conn.close()


def test_unknown_spawn_error_requeues_with_persisted_retry_decision(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="unknown startup", assignee="default")
        kb.recompute_ready(conn, failure_limit=3)
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")

        def fail_spawn(_task, _workspace):
            raise RuntimeError("uncertain startup failure")

        result = kb.dispatch_once(
            conn,
            spawn_fn=fail_spawn,
            failure_limit=3,
            reconcile_orphans=False,
        )

        task = kb.get_task(conn, task_id)
        assert result.auto_blocked == []
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        run = kb.list_runs(conn, task_id)[0]
        assert run.outcome == "spawn_failed"
        assert run.metadata["reason_code"] == "worker_startup_unknown"
        assert run.metadata["retryable"] is True
    finally:
        conn.close()


def test_unknown_spawn_error_at_breaker_persists_effective_no_retry(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="terminal unknown startup", assignee="default")
        kb.recompute_ready(conn, failure_limit=1)
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")

        def fail_spawn(_task, _workspace):
            raise RuntimeError("uncertain startup failure")

        result = kb.dispatch_once(
            conn,
            spawn_fn=fail_spawn,
            failure_limit=1,
            reconcile_orphans=False,
        )

        assert result.auto_blocked == [task_id]
        run = kb.list_runs(conn, task_id)[0]
        assert run.metadata["retryable"] is False
        gave_up = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "gave_up"
        ]
        assert len(gave_up) == 1
        assert gave_up[0].payload["retryable"] is False
    finally:
        conn.close()
