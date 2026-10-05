"""Iteration-budget exhaustion is a sizing failure, not a transient one.

Incident (t_72f77172): a card that exhausted its 45-turn budget was recorded as an
ordinary retryable ``timed_out``; the dispatcher reclaimed it 19 seconds later and
burned a second full 45/45 run into the same wall. These tests pin the corrected
contract against a real board DB:

* exhaustion blocks the card on the FIRST occurrence (sticky ``gave_up``), so
  ``recompute_ready`` / the dispatcher never re-claim it automatically;
* the event and run carry ``reason_code``/``retryable=False``/budget metadata;
* a stale worker (run N) cannot block or close a newer run N+1;
* an explicit operator ``unblock`` still releases it;
* per-card ``max_iterations`` is stored and reaches the worker argv as a
  ``chat --max-turns`` flag the real parser accepts.
"""

from __future__ import annotations

import json
import logging
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


def _claim(title: str = "oversized card", **kw):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="gohanlite", **kw)
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id
        return tid, int(claimed.current_run_id)


def _exhaust(monkeypatch, tid: str, run_id, used: int = 45, budget: int = 45, summary=None):
    from agent.turn_finalizer import _record_kanban_budget_exhausted

    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    _record_kanban_budget_exhausted(
        tid, used, budget, logging.getLogger("test"), handoff_summary=summary,
    )


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"] or "{}")
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (tid, kind),
        )
    ]


def test_first_exhaustion_blocks_and_is_not_reclaimed(kanban_home, monkeypatch):
    tid, run_id = _claim()
    _exhaust(monkeypatch, tid, run_id, summary="did steps 1-3 of 9")

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert "split" in (task.last_failure_error or "").lower()

        # The incident: the next dispatcher tick re-promoted the card. It must not.
        for _ in range(3):
            kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.claim_task(conn, tid) is None

        gave_up = _events(conn, tid, "gave_up")
        assert len(gave_up) == 1
        ev = gave_up[0]
        assert ev["sticky"] is True
        assert ev["retryable"] is False
        assert ev["reason_code"] == "iteration_budget_exhausted"
        assert ev["block_cause"] == "iteration_budget_exhausted"
        assert ev["trigger_outcome"] == "iteration_budget_exhausted"
        assert ev["budget_used"] == 45 and ev["budget_max"] == 45
        assert "split" in ev["operator_hint"].lower()
        # No ordinary retryable timed_out event is emitted for this run.
        assert _events(conn, tid, "timed_out") == []

        run = conn.execute(
            "SELECT outcome, ended_at, summary, metadata FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert run["ended_at"] is not None
        assert run["outcome"] == "gave_up"
        assert run["summary"] == "did steps 1-3 of 9"
        meta = json.loads(run["metadata"])
        assert meta["retryable"] is False
        assert meta["reason_code"] == "iteration_budget_exhausted"


def test_stale_run_cannot_block_newer_run(kanban_home, monkeypatch):
    tid, old_run = _claim()
    with kbc.connect_closing() as conn:
        # Run N is closed out from under its worker; the dispatcher claims run N+1.
        conn.execute("UPDATE task_runs SET ended_at = 1, outcome = 'reclaimed', status = 'reclaimed' WHERE id = ?",
                     (old_run,))
        conn.execute("UPDATE tasks SET status = 'ready', current_run_id = NULL, claim_lock = NULL WHERE id = ?",
                     (tid,))
        conn.commit()
        new = kb.claim_task(conn, tid)
        assert new is not None and int(new.current_run_id) != old_run

    _exhaust(monkeypatch, tid, old_run)

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert int(task.current_run_id) == int(new.current_run_id)
        assert _events(conn, tid, "gave_up") == []
        live = conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (new.current_run_id,)).fetchone()
        assert live["ended_at"] is None


def test_duplicate_finalizer_callback_is_idempotent(kanban_home, monkeypatch):
    tid, run_id = _claim()
    _exhaust(monkeypatch, tid, run_id)
    _exhaust(monkeypatch, tid, run_id)
    with kbc.connect_closing() as conn:
        assert len(_events(conn, tid, "gave_up")) == 1
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_explicit_unblock_releases_the_card(kanban_home, monkeypatch):
    tid, run_id = _claim()
    _exhaust(monkeypatch, tid, run_id)
    with kbc.connect_closing() as conn:
        assert kb.unblock_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_review_lane_exhaustion_resumes_to_review_on_unblock(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="impl", assignee="gohanlite")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="built", expected_run_id=claimed.current_run_id)
        reviewer = kb.claim_review_task(conn, tid)
        assert reviewer is not None and reviewer.current_run_id
    _exhaust(monkeypatch, tid, int(reviewer.current_run_id))
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "review"


def test_missing_run_identity_still_records_terminal_block(kanban_home, monkeypatch):
    """Legacy/manual workers without HERMES_KANBAN_RUN_ID keep the pre-existing
    guarantee that exhaustion records a terminal outcome (no ambiguous running row)."""
    tid, _run_id = _claim()
    _exhaust(monkeypatch, tid, None)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("raw", ["abc", "0", "-3"])
def test_malformed_run_identity_does_not_touch_the_card(kanban_home, monkeypatch, raw):
    tid, _run_id = _claim()
    _exhaust(monkeypatch, tid, raw)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert _events(conn, tid, "gave_up") == []


# ---------------------------------------------------------------------------
# Per-card max_iterations plumbing
# ---------------------------------------------------------------------------


def test_create_task_persists_max_iterations(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="small", assignee="gohanlite", max_iterations=20, max_retries=1)
        task = kb.get_task(conn, tid)
        assert task.max_iterations == 20
        assert task.max_retries == 1
        plain = kb.get_task(conn, kb.create_task(conn, title="default", assignee="gohanlite"))
        assert plain.max_iterations is None


@pytest.mark.parametrize("bad", [0, -1])
def test_create_task_rejects_non_positive_budgets(kanban_home, bad):
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match="max_iterations"):
            kb.create_task(conn, title="x", assignee="a", max_iterations=bad)
        with pytest.raises(ValueError, match="max_retries"):
            kb.create_task(conn, title="x", assignee="a", max_retries=bad)


def _argv_task(**overrides) -> kb.Task:
    base = dict(
        id="t_budget", title="t", body=None, assignee="gohanlite", status="running", priority=0,
        created_by=None, created_at=1, started_at=None, completed_at=None,
        workspace_kind="scratch", workspace_path=None, claim_lock=None, claim_expires=None, tenant=None,
    )
    base.update(overrides)
    return kb.Task(**base)


def test_worker_argv_passes_card_budget_as_chat_flag(monkeypatch):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda home: None)
    argv = kbd._worker_argv(_argv_task(max_iterations=20), "gohanlite", None)
    chat = argv.index("chat")
    # Never as a global (pre-subcommand) flag: that crashed workers with exit 2.
    assert "--max-turns" not in argv[:chat]
    assert argv[chat + 1: chat + 3] == ["--max-turns", "20"]

    from hermes_cli._parser import build_top_level_parser
    parser = build_top_level_parser()[0]
    # -p/--profile is consumed before the top-level argparse parser runs; feed
    # the parser the post-preparse argv starting at the chat subcommand.
    parsed = parser.parse_args(argv[chat:])
    assert parsed.max_turns == 20
    assert parsed.query == "work kanban task t_budget"


def test_worker_argv_omits_budget_flag_when_unset(monkeypatch):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda home: None)
    argv = kbd._worker_argv(_argv_task(), "gohanlite", None)
    assert "--max-turns" not in argv
