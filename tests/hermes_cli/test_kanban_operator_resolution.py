"""Regression coverage for operator recovery and lifecycle diagnostics."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as db:
        yield db


def _task(conn, title: str, **kwargs) -> str:
    return kb.create_task(conn, title=title, assignee="worker", **kwargs)


def test_resolve_triage_done_releases_dependents_and_audits(conn):
    replacement = _task(conn, "replacement")
    assert kb.complete_task(conn, replacement, summary="replacement delivered")
    parent = _task(conn, "stranded", triage=True)
    child = _task(conn, "dependent", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"

    outcome = kb.resolve_task(
        conn,
        parent,
        disposition="done",
        reason="Work completed by the replacement implementation.",
        replacement_task_id=replacement,
        actor="default",
    )

    assert outcome.ok is True
    assert outcome.source_status == "triage"
    assert outcome.status == "done"
    assert kb.get_task(conn, parent).status == "done"
    assert kb.get_task(conn, child).status == "ready"
    event = kb.list_events(conn, parent)[-1]
    assert event.kind == "completed"
    assert event.payload["operator_resolution"] is True
    assert event.payload["reason"] == "Work completed by the replacement implementation."
    assert event.payload["replacement_task_id"] == replacement
    assert event.payload["actor"] == "default"


def test_resolve_triage_archived_releases_dependents(conn):
    parent = _task(conn, "obsolete", triage=True)
    child = _task(conn, "dependent", parents=[parent])

    outcome = kb.resolve_task(
        conn,
        parent,
        disposition="archived",
        reason="Superseded and no longer needed.",
        actor="default",
    )

    assert outcome.ok is True
    assert kb.get_task(conn, parent).status == "archived"
    assert kb.get_task(conn, child).status == "ready"
    event = kb.list_events(conn, parent)[-1]
    assert event.kind == "archived"
    assert event.payload["operator_resolution"] is True


def test_resolve_rejects_nonterminal_replacement_without_mutation(conn):
    parent = _task(conn, "stranded", triage=True)
    replacement = _task(conn, "still running")

    outcome = kb.resolve_task(
        conn,
        parent,
        disposition="done",
        reason="Claimed replacement.",
        replacement_task_id=replacement,
        actor="default",
    )

    assert outcome.ok is False
    assert outcome.reason_code == "replacement_not_terminal"
    assert kb.get_task(conn, parent).status == "triage"


def test_resolve_rejects_normal_blocked_task(conn):
    task_id = _task(conn, "ordinary block")
    assert kb.block_task(conn, task_id, reason="waiting for input", kind="needs_input")

    outcome = kb.resolve_task(
        conn,
        task_id,
        disposition="done",
        reason="operator tried to bypass ordinary work",
        actor="default",
    )

    assert outcome.ok is False
    assert outcome.reason_code == "source_status_not_resolvable"
    assert kb.get_task(conn, task_id).status == "blocked"


def test_goal_rejection_fence_blocks_and_ends_owned_run(conn):
    task_id = _task(conn, "goal task", goal_mode=True)
    assert kb.claim_task(conn, task_id, claimer="worker:test")
    run_id = kb.get_task(conn, task_id).current_run_id

    assert kb.fence_goal_rejection(
        conn,
        task_id,
        reason="verification evidence is missing",
        verdict="continue",
        expected_run_id=run_id,
    )

    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.current_run_id is None
    assert task.claim_lock is None
    run = kb.latest_run(conn, task_id)
    assert run.outcome == "blocked"
    event = kb.list_events(conn, task_id)[-1]
    assert event.kind == "blocked"
    assert event.payload["manual_resolution_required"] is True
    assert event.payload["retryable"] is False
    assert event.payload["cause"] == "goal_completion_rejected"
    assert event.payload["source_status"] == "running"

    outcome = kb.resolve_task(
        conn,
        task_id,
        disposition="done",
        reason="Operator verified the external evidence.",
        actor="default",
    )
    assert outcome.ok is True
    assert kb.get_task(conn, task_id).status == "done"


def test_goal_rejection_fence_is_run_id_guarded(conn):
    task_id = _task(conn, "goal task", goal_mode=True)
    assert kb.claim_task(conn, task_id, claimer="worker:test")

    assert not kb.fence_goal_rejection(
        conn,
        task_id,
        reason="stale worker",
        verdict="continue",
        expected_run_id=999_999,
    )
    assert kb.get_task(conn, task_id).status == "running"


def test_transition_failure_reports_state_reason_and_recovery(conn):
    task_id = _task(conn, "triaged", triage=True)

    failure = kb.explain_transition_failure(conn, task_id, "complete")

    assert failure["task_id"] == task_id
    assert failure["requested_transition"] == "complete"
    assert failure["current_status"] == "triage"
    assert failure["reason_code"] == "source_status_not_allowed"
    assert "running" in failure["allowed_from"]
    assert any("kanban resolve" in action for action in failure["recovery_actions"])


def test_transition_failure_distinguishes_not_found_and_stale_run(conn):
    missing = kb.explain_transition_failure(conn, "t_missing", "complete")
    assert missing["current_status"] is None
    assert missing["reason_code"] == "task_not_found"

    task_id = _task(conn, "running")
    assert kb.claim_task(conn, task_id, claimer="worker:test")
    stale = kb.explain_transition_failure(
        conn, task_id, "complete", expected_run_id=999_999
    )
    assert stale["current_status"] == "running"
    assert stale["reason_code"] == "stale_run_id"
    assert stale["current_run_id"] == kb.get_task(conn, task_id).current_run_id


def test_cli_resolve_is_operator_only(conn, monkeypatch, capsys):
    from hermes_cli.kanban import _cmd_resolve

    task_id = _task(conn, "stranded", triage=True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    args = argparse.Namespace(
        task_id=task_id,
        disposition="done",
        reason="verified externally",
        replacement=None,
        json=False,
    )

    assert _cmd_resolve(args) != 0
    assert "operator-only" in capsys.readouterr().err
    assert kb.get_task(conn, task_id).status == "triage"
