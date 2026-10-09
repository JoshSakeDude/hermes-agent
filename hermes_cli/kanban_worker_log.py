"""Attempt-scoped Kanban worker log diagnostics."""

from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER


_EXIT_TRAILER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"(\d+)\s*$", re.MULTILINE,
)
_LOG_CHROME = re.compile(r"[─━═╭╮╰╯│┃┌┐└┘]+|☤\s*Hermes")
_WORKER_ATTEMPT_MARKER_PREFIX = "[hermes-kanban-worker-attempt run="


def _read_worker_log(task_id: str, board: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_db

    try:
        return kanban_db.read_worker_log(task_id, tail_bytes=4000, board=board)
    except (OSError, RuntimeError, ValueError):
        return None


def worker_log_exit_code(task_id: str, board: Optional[str] = None) -> Optional[int]:
    """Return the current attempt's durable worker exit code, when present."""
    matches = _EXIT_TRAILER_RE.findall(_current_attempt(_read_worker_log(task_id, board) or ""))
    return int(matches[-1]) if matches else None


def _current_attempt(raw: str) -> str:
    marker = raw.rfind(_WORKER_ATTEMPT_MARKER_PREFIX)
    if marker == -1:
        return raw
    marker_end = raw.find("\n", marker)
    return raw[marker_end + 1:] if marker_end != -1 else ""


def worker_final_output(task_id: str, board: Optional[str] = None) -> str:
    """Return only the current attempt's final useful worker output.

    Logs stay append-only for operator history, while an attempt marker prevents
    retry diagnostics from folding earlier attempts into each new ``gave_up``
    error. Legacy logs without a marker retain their previous best-effort tail.
    """
    raw = _read_worker_log(task_id, board)
    if not raw:
        return ""
    raw = _EXIT_TRAILER_RE.sub("", _current_attempt(raw))

    from agent.i18n import t

    cut = raw.rfind(t("cli.session.exit_resume_hint"))
    if cut != -1:
        raw = raw[:cut]
    noise_prefixes = ("session_id:", "Query:", t("cli.chat.initializing_agent"))
    lines = []
    for line in raw.splitlines():
        line = _LOG_CHROME.sub("", line).strip()
        if line and not line.startswith(noise_prefixes):
            lines.append(line)
    return " ".join(lines)[-400:]


def open_worker_log(
    task_id: str,
    run_id: Optional[int],
    *,
    board: Optional[str],
    rotation_config: Callable[[], tuple[int, int]],
    rotate: Callable[[Path, int, int], None],
) -> BinaryIO:
    """Open the append log and delimit the newly spawned worker attempt."""
    from hermes_cli import kanban_db

    log_dir = kanban_db.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}.log"
    rotate_bytes, backup_count = rotation_config()
    rotate(log_path, rotate_bytes, backup_count)
    log_f = open(log_path, "ab")
    marker_run_id = run_id if run_id is not None else "unknown"
    log_f.write(f"\n{_WORKER_ATTEMPT_MARKER_PREFIX}{marker_run_id}]\n".encode())
    log_f.flush()
    return log_f
