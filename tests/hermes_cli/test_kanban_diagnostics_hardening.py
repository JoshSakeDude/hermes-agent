"""Diagnostics for narrow Kanban hardening terminal reasons."""
from __future__ import annotations

import pytest

from hermes_cli.kanban_diagnostics import DIAGNOSTIC_KINDS, compute_task_diagnostics


@pytest.mark.parametrize(
    ("reason_code", "title_text", "action_text"),
    [
        (
            "iteration_budget_exhausted",
            "iteration budget",
            "Resize or split",
        ),
        (
            "worker_cli_invalid_max_turns_35",
            "launch command was invalid",
            "Check worker launch arguments",
        ),
    ],
)
def test_actionable_hardening_reason_has_distinct_no_retry_diagnostic(
    reason_code, title_text, action_text,
):
    task = {
        "id": "t_diag",
        "status": "blocked",
        "assignee": "default",
        "consecutive_failures": 1,
        "blocked_at": 100,
    }
    runs = [{
        "id": 9,
        "ended_at": 100,
        "outcome": "gave_up",
        "metadata": {
            "reason_code": reason_code,
            "retryable": False,
            "operator_hint": "safe next action",
        },
    }]

    diagnostics = compute_task_diagnostics(
        task, [], runs, now=100, config={"blocked_threshold_seconds": 9999},
    )
    matching = [diag for diag in diagnostics if diag.kind == reason_code]

    assert len(matching) == 1
    diagnostic = matching[0]
    assert title_text in diagnostic.title
    assert "no retry" in diagnostic.title
    assert diagnostic.run_id == 9
    assert diagnostic.data["retryable"] is False
    assert diagnostic.actions[0].suggested is True
    assert action_text in diagnostic.actions[0].label


def test_actionable_reason_diagnostic_does_not_guess_for_unknown_code():
    diagnostics = compute_task_diagnostics(
        {
            "id": "t_unknown",
            "status": "blocked",
            "consecutive_failures": 1,
            "blocked_at": 100,
        },
        [],
        [{
            "id": 10,
            "ended_at": 100,
            "outcome": "gave_up",
            "metadata": {
                "reason_code": "worker_startup_unknown",
                "retryable": True,
            },
        }],
        now=100,
        config={"blocked_threshold_seconds": 9999},
    )

    assert not any(
        diag.kind in {
            "iteration_budget_exhausted",
            "worker_cli_invalid_max_turns_35",
        }
        for diag in diagnostics
    )


def test_actionable_terminal_reason_does_not_reuse_stale_historical_run():
    diagnostics = compute_task_diagnostics(
        {
            "id": "t_stale",
            "status": "blocked",
            "consecutive_failures": 0,
            "blocked_at": 200,
        },
        [],
        [
            {
                "id": 1,
                "ended_at": 100,
                "metadata": {"reason_code": "iteration_budget_exhausted"},
            },
            {
                "id": 2,
                "ended_at": 200,
                "metadata": {"reason_code": "unrelated_failure"},
            },
        ],
        now=200,
        config={"blocked_threshold_seconds": 9999},
    )
    assert not any(
        diag.kind in {
            "iteration_budget_exhausted",
            "worker_cli_invalid_max_turns_35",
        }
        for diag in diagnostics
    )


def test_new_terminal_reason_kinds_are_advertised():
    assert "iteration_budget_exhausted" in DIAGNOSTIC_KINDS
    assert "worker_cli_invalid_max_turns_35" in DIAGNOSTIC_KINDS
