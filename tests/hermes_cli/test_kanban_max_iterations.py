"""Per-card iteration budget (Fix B) + iteration-cap-routes-to-blocked (retry-fix).

Covers:
  * ``max_iterations`` round-trips through create_task → persist → get_task.
  * When set, ``_default_spawn`` exports HERMES_MAX_ITERATIONS into the worker
    env; when unset it injects nothing (worker resolves the global default).
  * Iteration-cap exhaustion routes the task to ``blocked`` (force_trip),
    NOT the below-threshold auto-retry ``ready`` phase, and preserves the
    reviewer-run guard.
"""
from __future__ import annotations

import subprocess
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
# Fix B — persistence round-trip
# ---------------------------------------------------------------------------


def test_max_iterations_round_trips_create_persist_show(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="big task", assignee="gohanlite", max_iterations=80,
        )
        task = kb.get_task(conn, t)
        assert task.max_iterations == 80


def test_max_iterations_unset_defaults_to_none(kanban_home):
    """A card created without the field must persist NULL, so the worker
    falls through to the global default (existing behaviour unchanged)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="normal task", assignee="gohanlite")
        task = kb.get_task(conn, t)
        assert task.max_iterations is None


# ---------------------------------------------------------------------------
# Fix B — spawn-env injection
# ---------------------------------------------------------------------------


def _make_task(kb_mod, *, max_iterations):
    return kb_mod.Task(
        id="t_iter_budget",
        title="iter budget",
        body=None,
        assignee="gohanlite",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
        max_iterations=max_iterations,
    )


def _capture_spawn_env(monkeypatch, tmp_path, *, max_iterations):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "gohanlite"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "agent:\n  max_turns: 12\ntoolsets:\n  - hermes-cli\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(
        _make_task(kb, max_iterations=max_iterations), str(workspace),
    )
    return captured


def test_default_spawn_exports_max_iterations_when_set(monkeypatch, tmp_path):
    env = _capture_spawn_env(monkeypatch, tmp_path, max_iterations=80)["env"]
    assert env["HERMES_MAX_ITERATIONS"] == "80"


def test_default_spawn_passes_task_max_iterations_as_cli_override(
    monkeypatch, tmp_path,
):
    """The task cap must beat the worker profile's agent.max_turns setting.

    ``HermesCLI`` intentionally gives profile config precedence over the
    HERMES_MAX_ITERATIONS environment bridge, so env-only propagation silently
    turns a 30-turn card into (for example) a 45-turn worker.  The explicit CLI
    flag is the documented highest-precedence channel.
    """
    captured = _capture_spawn_env(monkeypatch, tmp_path, max_iterations=30)
    cmd = captured["cmd"]
    index = cmd.index("--max-turns")
    assert cmd[index + 1] == "30"
    assert index < cmd.index("chat")


def test_default_spawn_passes_card_override_before_chat(monkeypatch, tmp_path):
    captured = _capture_spawn_env(monkeypatch, tmp_path, max_iterations=35)
    cmd = captured["cmd"]

    flag_index = cmd.index("--max-turns")
    assert cmd[flag_index + 1] == "35"
    assert flag_index < cmd.index("chat")


def test_exact_35_turn_worker_argv_crosses_real_parser_boundary(
    monkeypatch, tmp_path,
):
    """Regression for ``invalid choice: '35'`` from the live incident."""
    import sys
    from hermes_cli import main as hermes_main

    cmd = _capture_spawn_env(
        monkeypatch, tmp_path, max_iterations=35,
    )["cmd"]
    captured = {}
    monkeypatch.setattr(
        hermes_main, "cmd_chat",
        lambda args: captured.update(command=args.command, max_turns=args.max_turns),
    )
    monkeypatch.setattr(hermes_main, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(sys, "argv", cmd)

    # The console entry point strips --profile/-p before building the real
    # argparse tree. Mock only the post-parse command handler.
    hermes_main._apply_profile_override()
    hermes_main.main()

    assert captured == {"command": "chat", "max_turns": 35}


def test_default_spawn_omits_max_iterations_when_unset(monkeypatch, tmp_path):
    """Unset card must NOT pin HERMES_MAX_ITERATIONS, so the worker resolves
    the global agent.max_turns default exactly as before this change.

    The dispatcher scrubs session-routing ContextVars but otherwise inherits
    os.environ; assert the card injected nothing rather than that the key is
    globally absent.
    """
    monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
    captured = _capture_spawn_env(monkeypatch, tmp_path, max_iterations=None)
    env = captured["env"]
    assert "HERMES_MAX_ITERATIONS" not in env
    assert "--max-turns" not in captured["cmd"]


# ---------------------------------------------------------------------------
# Retry-fix — iteration-cap exhaustion routes to blocked, not auto-retry
# ---------------------------------------------------------------------------


def test_iteration_exhaustion_routes_to_blocked_not_ready(kanban_home, monkeypatch):
    """A running task whose worker exhausts its iteration budget must land in
    ``blocked`` (human decision: resize/split), NOT be re-queued ``ready`` to
    blind-retry into the same wall."""
    from agent.turn_finalizer import _record_kanban_budget_exhausted
    import logging

    with kb.connect() as conn:
        t = kb.create_task(conn, title="too big", assignee="gohanlite")
        host = kb._claimer_id().split(":", 1)[0]
        claimed = kb.claim_task(conn, t, claimer=f"{host}:worker")
        assert claimed is not None and claimed.current_run_id is not None
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    # The worker path connects on its own; point it at this board's DB.
    _record_kanban_budget_exhausted(
        t, api_call_count=45, max_iterations=45,
        logger=logging.getLogger("test"),
        handoff_summary="Changed approval.py; tests pending; resume in workspace A.",
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "blocked", (
            f"iteration-cap exhaustion should block, got {task.status!r}"
        )
        # A gave_up event should have been emitted with the block cause.
        events = kb.list_events(conn, t)
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert gave_up, "expected a gave_up event on iteration-cap block"
        payload = gave_up[-1].payload or {}
        assert payload.get("block_cause") == "iteration_budget_exhausted"
        # Reason must be structured + human-readable, no secrets.
        assert "resize max_iterations" in (task.last_failure_error or "")

        # A max-iteration exhaustion is not transient. It must remain blocked
        # after the dispatcher's normal promotion pass instead of immediately
        # respawning the same unchanged card.
        assert task.block_kind == "needs_input"
        assert task.claim_lock is None
        assert task.current_run_id is None
        blocked_events = [e for e in events if e.kind == "blocked"]
        assert blocked_events
        assert (blocked_events[-1].payload or {}).get("kind") == "needs_input"
        assert payload.get("trigger_outcome") == "iteration_budget_exhausted"
        run = conn.execute(
            "SELECT outcome, summary FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert run["outcome"] == "gave_up"
        assert run["summary"] == (
            "Changed approval.py; tests pending; resume in workspace A."
        )

    with kb.connect() as conn:
        assert kb.recompute_ready(conn) == 0
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "blocked"

    # A duplicated finalizer callback for the same closed run is a no-op.
    _record_kanban_budget_exhausted(
        t, api_call_count=45, max_iterations=45,
        logger=logging.getLogger("test"),
        handoff_summary="duplicate callback",
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.consecutive_failures == 1
        events = kb.list_events(conn, t)
        assert len([e for e in events if e.kind == "gave_up"]) == 1
        assert len([e for e in events if e.kind == "blocked"]) == 1


def test_iteration_exhaustion_replaces_historical_block_cause(
    kanban_home, monkeypatch,
):
    """A prior capability block must not mask a later iteration exhaustion."""
    from agent.turn_finalizer import _record_kanban_budget_exhausted
    import logging

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="retry incident", assignee="gohanlite")
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:first") is not None
        assert kb.block_task(
            conn, task_id, reason="old access issue", kind="capability",
        )
        assert kb.unblock_task(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:second")
        assert claimed is not None and claimed.current_run_id is not None
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    _record_kanban_budget_exhausted(
        task_id, api_call_count=30, max_iterations=30,
        logger=logging.getLogger("test"),
        handoff_summary="partial implementation survives",
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        blocked = [e for e in kb.list_events(conn, task_id) if e.kind == "blocked"]
        assert (blocked[-1].payload or {}).get("block_cause") == (
            "iteration_budget_exhausted"
        )


# ---------------------------------------------------------------------------
# #2 — CLI + agent-tool surface expose max_iterations
# ---------------------------------------------------------------------------


def test_cli_create_accepts_max_iterations(kanban_home):
    """`kanban create --max-iterations N` persists the budget, and it shows
    up in both `show` text and `list --json`."""
    from hermes_cli import kanban as kc

    out = kc.run_slash(
        "create 'big cli task' --assignee gohanlite "
        "--max-iterations 80 --json"
    )
    import json as _json
    created = _json.loads(out)
    assert created["max_iterations"] == 80

    with kb.connect() as conn:
        task = kb.get_task(conn, created["id"])
        assert task.max_iterations == 80

    show = kc.run_slash(f"show {created['id']}")
    assert "max-iterations: 80 (task)" in show


def test_cli_create_rejects_nonpositive_max_iterations(kanban_home):
    from hermes_cli import kanban as kc

    rc = kc.run_slash("create x --assignee a --max-iterations 0")
    # run_slash returns the printed output; the arg-validation path prints an
    # error to stderr and returns exit code 2 — assert nothing was created.
    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
        assert not any(t.title == "x" for t in tasks)


def test_kanban_create_tool_schema_exposes_max_iterations():
    """The agent-facing tool schema must advertise the new field so the
    leader can set it from a kanban_create call."""
    from tools import kanban_tools

    spec = kanban_tools.KANBAN_CREATE_SCHEMA
    props = spec["parameters"]["properties"]
    assert "max_iterations" in props
    assert props["max_iterations"]["type"] == "integer"

