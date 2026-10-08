"""DB-backed contracts for Kanban iteration-budget exhaustion.

Iteration exhaustion is a card-sizing failure, not a transient worker timeout.
These tests deliberately use a real temporary SQLite board: only the finalizer's
external model/tool calls are absent.  The deployed pin is expected to fail
these assertions until the r0216 behavior is restored.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _claim(title: str = "oversized card", **kwargs) -> tuple[str, int]:
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title=title, assignee="gohanlite", **kwargs)
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        return task_id, int(claimed.current_run_id)


def _exhaust(
    monkeypatch: pytest.MonkeyPatch,
    task_id: str,
    run_id: int | str | None,
    *,
    used: int = 45,
    budget: int = 45,
    summary: str | None = None,
) -> None:
    from agent.turn_finalizer import _record_kanban_budget_exhausted

    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    args = (
        task_id,
        used,
        budget,
        logging.getLogger("test.iteration_exhaustion"),
    )
    if summary is None:
        _record_kanban_budget_exhausted(*args)
    else:
        _record_kanban_budget_exhausted(*args, handoff_summary=summary)


def _events(conn, task_id: str, kind: str) -> list[dict]:
    return [
        json.loads(row["payload"] or "{}")
        for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        )
    ]


def test_first_exhaustion_sticky_blocks_instead_of_timing_out(kanban_home, monkeypatch):
    task_id, run_id = _claim()
    _exhaust(monkeypatch, task_id, run_id)

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert "split" in (task.last_failure_error or "").lower()

        for _ in range(3):
            kb.recompute_ready(conn)
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.claim_task(conn, task_id) is None

        assert _events(conn, task_id, "timed_out") == []
        gave_up = _events(conn, task_id, "gave_up")
        assert len(gave_up) == 1
        assert gave_up[0]["sticky"] is True
        assert gave_up[0]["retryable"] is False
        assert gave_up[0]["reason_code"] == "iteration_budget_exhausted"
        assert gave_up[0]["budget_used"] == 45
        assert gave_up[0]["budget_max"] == 45

        run = conn.execute(
            "SELECT outcome, ended_at, metadata FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert run["ended_at"] is not None
        assert run["outcome"] == "gave_up"
        metadata = json.loads(run["metadata"])
        assert metadata["retryable"] is False
        assert metadata["reason_code"] == "iteration_budget_exhausted"


def test_exhaustion_keeps_handoff_summary(kanban_home, monkeypatch):
    task_id, run_id = _claim()
    _exhaust(monkeypatch, task_id, run_id, summary="finished parser; API remains")

    with kbc.connect_closing() as conn:
        run = conn.execute(
            "SELECT summary FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run["summary"] == "finished parser; API remains"


def test_stale_run_exhaustion_is_ignored(kanban_home, monkeypatch):
    task_id, stale_run_id = _claim()
    with kbc.connect_closing() as conn:
        conn.execute(
            "UPDATE task_runs SET ended_at = 1, outcome = 'reclaimed', status = 'reclaimed' WHERE id = ?",
            (stale_run_id,),
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, claim_lock = NULL WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        live = kb.claim_task(conn, task_id)
        assert live is not None and int(live.current_run_id) != stale_run_id
        live_run_id = int(live.current_run_id)

    _exhaust(monkeypatch, task_id, stale_run_id)

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert int(task.current_run_id) == live_run_id
        assert _events(conn, task_id, "gave_up") == []
        assert conn.execute(
            "SELECT count(*) FROM tasks WHERE idempotency_key = ?",
            (f"split:{task_id}",),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?", (live_run_id,)
        ).fetchone()["ended_at"] is None


def test_duplicate_exhaustion_is_ignored_and_creates_one_split_followup(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"orchestrator_profile": "planner"}},
    )
    task_id, run_id = _claim(
        title="large card",
        body="finish remaining work",
        tenant="tenant-a",
        session_id="session-a",
    )
    _exhaust(monkeypatch, task_id, run_id)
    _exhaust(monkeypatch, task_id, run_id)

    with kbc.connect_closing() as conn:
        assert len(_events(conn, task_id, "gave_up")) == 1
        assert kb.get_task(conn, task_id).consecutive_failures == 1
        followups = conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (f"split:{task_id}",)
        ).fetchall()
        assert len(followups) == 1
        followup = followups[0]
        assert followup["assignee"] == "planner"
        assert followup["tenant"] == "tenant-a"
        assert followup["session_id"] == "session-a"
        assert followup["max_iterations"] == 25
        assert followup["goal_mode"] == 0
        assert followup["workspace_kind"] == "scratch"
        assert followup["status"] == "ready"
        assert _events(conn, task_id, "split_followup_created") == [
            {"followup_task_id": followup["id"]}
        ]


def test_create_task_persists_per_card_max_iterations(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="small card",
            assignee="gohanlite",
            max_iterations=20,
        )
        assert kb.get_task(conn, task_id).max_iterations == 20
        default_id = kb.create_task(conn, title="default card", assignee="gohanlite")
        assert kb.get_task(conn, default_id).max_iterations is None


def _argv_task(**overrides) -> kb.Task:
    values = dict(
        id="t_budget",
        title="budget",
        body=None,
        assignee="gohanlite",
        status="running",
        priority=0,
        created_by=None,
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    values.update(overrides)
    return kb.Task(**values)


def test_worker_argv_passes_per_card_budget_after_chat(monkeypatch):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda home: None)
    argv = kbd._worker_argv(_argv_task(max_iterations=20), "gohanlite", None)
    chat = argv.index("chat")
    assert "--max-turns" not in argv[:chat]
    assert argv[chat + 1 : chat + 3] == ["--max-turns", "20"]

    from hermes_cli._parser import build_top_level_parser

    parser = build_top_level_parser()[0]
    parsed = parser.parse_args(argv[chat:])
    assert parsed.max_turns == 20
    assert parsed.query == "work kanban task t_budget"


@pytest.mark.parametrize(
    ("budget", "expected"),
    [(20, ["--max-turns", "20"]), (None, [])],
)
def test_spawned_worker_argv_uses_only_explicit_card_budget(
    kanban_home, monkeypatch, tmp_path, budget, expected
):
    """Assert the command at the Popen boundary, not only the argv helper."""
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda home: None)
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda task, command: command)
    captured = {}

    class FakeProc:
        pid = 4245

    def fake_popen(command, *args, **kwargs):
        captured["command"] = list(command)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "worker"
    workspace.mkdir()
    task = _argv_task(max_iterations=budget)

    assert kbd._default_spawn(task, str(workspace)) == FakeProc.pid
    command = captured["command"]
    chat = command.index("chat")
    assert "--max-turns" not in command[:chat]
    if budget is None:
        assert expected == []
        assert "--max-turns" not in command
    else:
        assert command[chat + 1 : chat + 3] == expected
