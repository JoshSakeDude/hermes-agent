"""Incident-level run ownership races for Kanban lifecycle finalization."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


_ERROR = "task too large for its budget; resize max_iterations or split the task"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _claimed_task(conn, *, title="race"):
    task_id = kb.create_task(conn, title=title, assignee="worker")
    task = kb.claim_task(conn, task_id)
    assert task is not None
    assert task.current_run_id is not None
    return task_id, task.current_run_id


def _exhaust(conn, task_id, run_id, *, summary="handoff"):
    return kb._record_task_failure(
        conn,
        task_id,
        error=_ERROR,
        outcome="iteration_budget_exhausted",
        force_trip=True,
        sticky_block_kind="needs_input",
        run_summary=summary,
        release_claim=True,
        end_run=True,
        expected_run_id=run_id,
        event_payload_extra={"block_cause": "iteration_budget_exhausted"},
    )


def test_completion_commit_wins_before_iteration_exhaustion(kanban_home):
    with kb.connect() as setup:
        task_id, run_id = _claimed_task(setup)

    with kb.connect() as completer:
        assert kb.complete_task(
            completer, task_id, summary="completed", expected_run_id=run_id,
        )
    with kb.connect() as finalizer:
        assert _exhaust(finalizer, task_id, run_id) is False

    with kb.connect() as check:
        task = kb.get_task(check, task_id)
        assert task is not None
        assert task.status == "done"
        assert task.current_run_id is None
        assert task.consecutive_failures == 0
        run = kb.list_runs(check, task_id)[0]
        assert run.outcome == "completed"
        kinds = [event.kind for event in kb.list_events(check, task_id)]
        assert "gave_up" not in kinds
        assert "blocked" not in kinds


def test_iteration_exhaustion_commit_wins_before_completion(kanban_home):
    with kb.connect() as setup:
        task_id, run_id = _claimed_task(setup)

    with kb.connect() as finalizer:
        assert _exhaust(finalizer, task_id, run_id, summary="resume here") is True
    with kb.connect() as completer:
        assert not kb.complete_task(
            completer, task_id, summary="late completion", expected_run_id=run_id,
        )

    with kb.connect() as check:
        task = kb.get_task(check, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.current_run_id is None
        assert task.consecutive_failures == 1
        run = kb.list_runs(check, task_id)[0]
        assert run.outcome == "gave_up"
        assert run.summary == "resume here"
        events = kb.list_events(check, task_id)
        assert len([event for event in events if event.kind == "gave_up"]) == 1
        assert len([event for event in events if event.kind == "blocked"]) == 1


def test_duplicate_iteration_finalizer_callback_is_noop(kanban_home):
    with kb.connect() as setup:
        task_id, run_id = _claimed_task(setup)

    with kb.connect() as first:
        assert _exhaust(first, task_id, run_id) is True
    with kb.connect() as duplicate:
        assert _exhaust(duplicate, task_id, run_id, summary="duplicate") is False

    with kb.connect() as check:
        task = kb.get_task(check, task_id)
        assert task is not None
        assert task.consecutive_failures == 1
        events = kb.list_events(check, task_id)
        assert len([event for event in events if event.kind == "gave_up"]) == 1
        assert len([event for event in events if event.kind == "blocked"]) == 1


def test_run_n_callback_cannot_close_or_clear_run_n_plus_one(kanban_home):
    with kb.connect() as setup:
        task_id, run_n = _claimed_task(setup)
        assert not kb._record_task_failure(
            setup,
            task_id,
            error="retryable",
            outcome="spawn_failed",
            failure_limit=3,
            release_claim=True,
            end_run=True,
            expected_run_id=run_n,
        )
        claimed = kb.claim_task(setup, task_id)
        assert claimed is not None
        run_n_plus_one = claimed.current_run_id
        assert run_n_plus_one is not None and run_n_plus_one != run_n

    with kb.connect() as stale:
        assert _exhaust(stale, task_id, run_n) is False

    with kb.connect() as check:
        task = kb.get_task(check, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.current_run_id == run_n_plus_one
        assert task.consecutive_failures == 1
        current = [run for run in kb.list_runs(check, task_id) if run.id == run_n_plus_one][0]
        assert current.ended_at is None
        assert current.outcome is None
        assert not [event for event in kb.list_events(check, task_id) if event.kind == "gave_up"]


def test_distinct_runs_with_same_sticky_block_kind_are_not_coalesced(kanban_home):
    with kb.connect() as first:
        task_id, run_one = _claimed_task(first)
        assert _exhaust(first, task_id, run_one, summary="first") is True
        assert kb.unblock_task(first, task_id)
        claimed = kb.claim_task(first, task_id)
        assert claimed is not None
        run_two = claimed.current_run_id
        assert run_two is not None and run_two != run_one

    with kb.connect() as second:
        assert _exhaust(second, task_id, run_two, summary="second") is True

    with kb.connect() as check:
        events = kb.list_events(check, task_id)
        gave_up = [event for event in events if event.kind == "gave_up"]
        blocked = [event for event in events if event.kind == "blocked"]
        assert [event.run_id for event in gave_up] == [run_one, run_two]
        assert [event.run_id for event in blocked] == [run_one, run_two]
        runs = kb.list_runs(check, task_id)
        assert [run.outcome for run in runs] == ["gave_up", "gave_up"]


def _replace_with_new_run(conn, task_id, run_id):
    assert not kb._record_task_failure(
        conn,
        task_id,
        error="retryable",
        outcome="spawn_failed",
        failure_limit=99,
        release_claim=True,
        end_run=True,
        expected_run_id=run_id,
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    assert claimed.current_run_id != run_id
    return claimed.current_run_id


def test_stale_spawn_success_cannot_attach_pid_to_newer_run(kanban_home):
    with kb.connect() as conn:
        task_id, run_n = _claimed_task(conn, title="stale spawn success")
        run_n_plus_one = _replace_with_new_run(conn, task_id, run_n)

        assert not kb._set_worker_pid(
            conn, task_id, 424242, expected_run_id=run_n,
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id == run_n_plus_one
        assert task.worker_pid is None
        current = [run for run in kb.list_runs(conn, task_id) if run.id == run_n_plus_one][0]
        assert current.worker_pid is None


def test_stale_spawn_failure_cannot_close_newer_run(kanban_home):
    with kb.connect() as conn:
        task_id, run_n = _claimed_task(conn, title="stale spawn failure")
        run_n_plus_one = _replace_with_new_run(conn, task_id, run_n)

        assert not kb._record_spawn_failure(
            conn,
            task_id,
            "late startup failure",
            failure_limit=1,
            expected_run_id=run_n,
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.current_run_id == run_n_plus_one
        current = [run for run in kb.list_runs(conn, task_id) if run.id == run_n_plus_one][0]
        assert current.ended_at is None
        assert current.outcome is None


def test_stale_token_budget_yield_cannot_requeue_newer_run(kanban_home):
    with kb.connect() as conn:
        task_id, run_n = _claimed_task(conn, title="stale token yield")
        run_n_plus_one = _replace_with_new_run(conn, task_id, run_n)

        status = kb._finalize_budget_yielded(
            conn,
            task_id,
            handoff_summary="late handoff",
            billable_tokens=500,
            max_total_tokens=500,
            expected_run_id=run_n,
        )

        assert status == "running"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id == run_n_plus_one
        assert task.budget_continuation_count == 0
        current = [run for run in kb.list_runs(conn, task_id) if run.id == run_n_plus_one][0]
        assert current.ended_at is None
        assert current.outcome is None


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_dispatcher_terminates_process_spawned_for_displaced_claim(
    kanban_home, monkeypatch, lane,
):
    terminated = []
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda pid, claim_lock: terminated.append((pid, claim_lock)) or {},
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"stale {lane} spawn", assignee="worker")
        if lane == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
            conn.commit()

        observed = {}

        def stale_spawn(claimed, _workspace):
            observed["claim_lock"] = claimed.claim_lock
            with kb.connect() as racer:
                assert not kb._record_spawn_failure(
                    racer,
                    claimed.id,
                    "superseded",
                    failure_limit=99,
                    expected_run_id=claimed.current_run_id,
                )
                replacement = (
                    kb.claim_review_task(racer, claimed.id)
                    if lane == "review"
                    else kb.claim_task(racer, claimed.id)
                )
                assert replacement is not None
                observed["replacement_run_id"] = replacement.current_run_id
            return 424242

        result = kb.dispatch_once(
            conn,
            spawn_fn=stale_spawn,
            reconcile_orphans=False,
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id == observed["replacement_run_id"]
        assert task.worker_pid is None
        assert result.spawned == []
        assert terminated == [(424242, observed["claim_lock"])]
