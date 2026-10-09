"""Transactional dependency handoff for exhausted Kanban split cards."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import reconcile_split_dependencies


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _event(conn, task_id: str, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, 1)",
        (task_id, kind, json.dumps(payload)),
    )
    conn.commit()


def _split_graph(conn):
    original = kb.create_task(conn, title="oversized", initial_status="blocked")
    downstream_a = kb.create_task(conn, title="publish", parents=[original])
    downstream_b = kb.create_task(conn, title="notify", parents=[original])
    split = kb.create_task(
        conn,
        title="split oversized card",
        idempotency_key=f"split:{original}",
        creator_task_id=original,
    )
    replacement_a = kb.create_task(conn, title="replacement A", creator_task_id=split)
    replacement_b = kb.create_task(conn, title="replacement B", creator_task_id=split)
    assert kb.complete_task(conn, replacement_a, summary="replacement A complete")
    assert kb.complete_task(conn, replacement_b, summary="replacement B complete")
    _event(conn, original, "gave_up", {"reason_code": "iteration_budget_exhausted"})
    _event(conn, original, "split_followup_created", {"followup_task_id": split})
    return original, downstream_a, downstream_b, replacement_a, replacement_b


def test_reconcile_split_dependencies_reparents_only_explicit_downstream_mapping(board):
    original, downstream_a, downstream_b, replacement_a, replacement_b = _split_graph(board)

    changed = reconcile_split_dependencies(
        board,
        original,
        {
            downstream_a: [replacement_a],
            downstream_b: [replacement_b],
        },
    )

    assert changed == 2
    assert kb.parent_ids(board, downstream_a) == [replacement_a]
    assert kb.parent_ids(board, downstream_b) == [replacement_b]
    assert kb.get_task(board, downstream_a).status == "ready"
    assert kb.get_task(board, downstream_b).status == "ready"


def test_reconcile_split_dependencies_rejects_ambiguous_mapping_without_mutation(board):
    original, downstream_a, downstream_b, replacement_a, _ = _split_graph(board)

    with pytest.raises(ValueError, match="every direct child"):
        reconcile_split_dependencies(
            board,
            original,
            {downstream_a: [replacement_a]},
        )

    assert kb.parent_ids(board, downstream_a) == [original]
    assert kb.parent_ids(board, downstream_b) == [original]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("running", "running downstream"),
        ("blocked", "approval-gated"),
        ("review", "approval-gated"),
    ],
)
def test_reconcile_split_dependencies_rejects_unsafe_downstream_statuses(
    board, status, message
):
    original, downstream_a, downstream_b, replacement_a, replacement_b = _split_graph(board)
    board.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, downstream_b))
    board.commit()

    with pytest.raises(ValueError, match=message):
        reconcile_split_dependencies(
            board,
            original,
            {
                downstream_a: [replacement_a],
                downstream_b: [replacement_b],
            },
        )

    assert kb.parent_ids(board, downstream_a) == [original]
    assert kb.parent_ids(board, downstream_b) == [original]


def test_reconcile_split_dependencies_rejects_cycle_without_partial_mutation(board):
    original, downstream_a, downstream_b, replacement_a, replacement_b = _split_graph(board)
    kb._link(board, downstream_b, replacement_b)
    board.commit()

    with pytest.raises(ValueError, match="cycle"):
        reconcile_split_dependencies(
            board,
            original,
            {
                downstream_a: [replacement_a],
                downstream_b: [replacement_b],
            },
        )

    assert kb.parent_ids(board, downstream_a) == [original]
    assert original in kb.parent_ids(board, downstream_b)


def test_reconcile_split_dependencies_rejects_cycle_created_by_combined_mapping(board):
    original, downstream_a, downstream_b, replacement_a, replacement_b = _split_graph(board)
    kb._link(board, downstream_a, replacement_b)
    kb._link(board, downstream_b, replacement_a)
    board.commit()

    with pytest.raises(ValueError, match="cycle"):
        reconcile_split_dependencies(
            board,
            original,
            {
                downstream_a: [replacement_a],
                downstream_b: [replacement_b],
            },
        )

    assert kb.parent_ids(board, downstream_a) == [original]
    assert kb.parent_ids(board, downstream_b) == [original]


def test_reconcile_split_dependencies_requires_completed_split_children(board):
    original, downstream_a, downstream_b, replacement_a, replacement_b = _split_graph(board)
    foreign = kb.create_task(board, title="unrelated completed task")
    assert kb.complete_task(board, foreign, summary="unrelated work complete")

    with pytest.raises(ValueError, match="not created by split follow-up"):
        reconcile_split_dependencies(
            board,
            original,
            {
                downstream_a: [replacement_a],
                downstream_b: [foreign],
            },
        )

    board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (replacement_b,))
    board.commit()
    with pytest.raises(ValueError, match="is not completed"):
        reconcile_split_dependencies(
            board,
            original,
            {
                downstream_a: [replacement_a],
                downstream_b: [replacement_b],
            },
        )

    assert kb.parent_ids(board, downstream_a) == [original]
    assert kb.parent_ids(board, downstream_b) == [original]
