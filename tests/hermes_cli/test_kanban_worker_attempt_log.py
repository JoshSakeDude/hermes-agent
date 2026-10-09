"""Regression tests for attempt-scoped Kanban worker logs."""

from __future__ import annotations

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
    kb.init_db()
    return home


def test_repeated_worker_attempts_scope_gave_up_output_to_latest_append_log(
    kanban_home, monkeypatch,
):
    """Each retry reads only its attempt from the append-mode worker log."""
    diagnostic = "hermes: no dependency environment is committed for this install"
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 1))

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="append log failure", assignee="default")
        for attempt in range(2):
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            log = kbd._open_worker_log(claimed, board=None)
            log.write(f"{diagnostic}\n".encode())
            log.close()

            dead = kbd._classify_dead_worker(
                4000 + attempt,
                claimed.claim_lock,
                task_id=tid,
                board=None,
            )
            tripped = kbd._record_task_failure(
                conn,
                tid,
                error=dead.error_text,
                outcome="crashed",
                failure_limit=2,
                expected_run_id=claimed.current_run_id,
                release_claim=True,
                end_run=True,
                event_payload_extra=dead.event_payload,
            )
            assert tripped is (attempt == 1)

        gave_up = next(
            event for event in reversed(kb.list_events(conn, tid))
            if event.kind == "gave_up"
        )
        assert gave_up.payload is not None
        error = gave_up.payload["error"]

    assert error.count("Worker's last output") == 1
    assert error.count(diagnostic) == 1
