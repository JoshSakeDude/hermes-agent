"""Notification subscriptions consumed by the gateway kanban-notifier: per-(task, platform, chat, thread) rows with delivery metadata, unseen-event cursors and purge of stale done-task subs.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Event


# Notifier reaction to a terminal event: "notify" = passive adapter.send only
# (default); "notify+wake" = send AND wake the destination agent; "wake" = wake only.
_NOTIFY_DELIVERY_MODES = ("notify", "notify+wake", "wake")

_SCALAR_TYPES = (str, int, float, bool)

# Origin-first routing markers persisted inside ``delivery_metadata`` (no schema
# change). ``route_*`` keys are internal bookkeeping and are stripped before any
# adapter send. ``route_role``: "origin" (the exact conversation that created the
# card), "fallback" (the single notify-only Alerts row created after the origin
# delivery fails or a Desktop update stays unread) or "mirror"; unstamped rows are classified
# by ``gateway.kanban_notify_routing.sub_role``.
ROUTE_ROLE_KEY = "route_role"
ROUTE_FAILURES_KEY = "route_failures"
ROUTE_ORIGIN_KEY = "route_origin"
ROUTE_FALLBACK_CAUSE_KEY = "route_fallback_cause"
FALLBACK_CAUSE_DESKTOP_UNREAD = "desktop_unread"
ROLE_ORIGIN, ROLE_MIRROR, ROLE_FALLBACK = "origin", "mirror", "fallback"
ROLE_ALERTS, ROLE_APPROVALS, ROLE_OPS = "alerts", "approvals", "ops"
FALLBACK_SENT_EVENT = "fallback_sent"

# Subscription primary key predicate; every per-row statement below binds
# ``(task_id, platform, chat_id, thread_id or "")`` against it.
_SUB_KEY_WHERE = "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?"


def _sub_key(task_id: str, platform: str, chat_id: str, thread_id: Optional[str]) -> tuple:
    return (task_id, platform, chat_id, thread_id or "")


def _encode_notify_delivery_metadata(metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Serialize platform send metadata stored on notification subscriptions."""
    if not isinstance(metadata, Mapping):
        return None
    clean = {
        str(key): value
        for key, value in metadata.items()
        if value is not None and isinstance(value, _SCALAR_TYPES)
    }
    if not clean:
        return None
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _decode_notify_delivery_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, _SCALAR_TYPES)}


def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_id_alt: Optional[str] = None,
    chat_type: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    delivery_mode: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Register a gateway source wanting terminal-state notifications for
    ``task_id``; idempotent on (task, platform, chat, thread).

    ``user_id_alt`` (Signal UUID, Feishu union_id, ...) and ``chat_type`` are
    replayed on active wake: ``build_session_key`` prefers the alt id, so
    omitting it would key the wake into a different session. ``None`` keeps an
    existing row's value. ``delivery_mode``: ``None`` leaves an existing row
    untouched, an explicit valid value is last-write-wins, unknown falls back
    to ``"notify"``. ``delivery_metadata`` merges supplied routing anchors
    into an existing row so re-subscribing never discards them. New subs start
    caught up (``last_event_id`` =
    ``MAX(task_events.id)``) so the notifier never replays history at boot.
    """
    valid_mode = delivery_mode if delivery_mode in _NOTIFY_DELIVERY_MODES else None
    # api_server is stateless: the adapter has no send(), the wake self-post IS
    # the delivery. A plain 'notify' default would leave those subs with no
    # delivery mechanism at all. Explicit modes still win.
    insert_mode = valid_mode or ("notify+wake" if platform == "api_server" else "notify")
    key = _sub_key(task_id, platform, chat_id, thread_id)
    with _kb.write_txn(conn):
        existing = conn.execute(
            "SELECT delivery_metadata FROM kanban_notify_subs " + _SUB_KEY_WHERE,
            key,
        ).fetchone()
        existing_metadata = _decode_notify_delivery_metadata(existing["delivery_metadata"]) if existing else {}
        merged_metadata = dict(existing_metadata)
        if delivery_metadata:
            merged_metadata.update(delivery_metadata)
        metadata_json = _encode_notify_delivery_metadata(merged_metadata) if merged_metadata else None
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
                 chat_type, notifier_profile, delivery_mode, delivery_metadata,
                 created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))
            """,
            (
                *key, user_id, user_id_alt, chat_type or "dm", notifier_profile,
                insert_mode, metadata_json, int(time.time()), task_id,
            ),
        )
        # chat_type / delivery_mode are last-write-wins; delivery metadata
        # preserves existing routing fields while supplied fields overwrite them.
        # user_id, user_id_alt and notifier_profile only self-heal legacy rows lacking one.
        for column, value, fill_only in (
            ("chat_type", chat_type, False),
            ("user_id", user_id, True),
            ("user_id_alt", user_id_alt, True),
            ("notifier_profile", notifier_profile, True),
            ("delivery_mode", valid_mode, False),
            ("delivery_metadata", metadata_json, False),
        ):
            if not value:
                continue
            guard = f" AND ({column} IS NULL OR {column} = '')" if fill_only else ""
            conn.execute(
                f"UPDATE kanban_notify_subs SET {column} = ? " + _SUB_KEY_WHERE + guard,
                (value, *key),
            )


def _notify_profile_filter(
    notifier_profiles: Optional[Iterable[str]],
    *,
    include_unowned: bool,
) -> tuple[str, list[str]]:
    """Build an optional SQL predicate for notification profile ownership."""
    if notifier_profiles is None:
        return "", []

    profiles = sorted({str(p).strip() for p in notifier_profiles if str(p).strip()})
    clauses: list[str] = []
    params: list[str] = []
    if profiles:
        clauses.append("notifier_profile IN (" + ",".join("?" for _ in profiles) + ")")
        params.extend(profiles)
    if include_unowned:
        clauses.append("notifier_profile IS NULL OR notifier_profile = ''")
    if not clauses:
        return "0", []
    return "(" + ") OR (".join(clauses) + ")", params


def list_notify_subs(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    *,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
) -> list[dict]:
    """List subscriptions, optionally restricted to notifier profile owners.

    No ``notifier_profiles`` -> all subscriptions. Gateway notifiers pass the
    profiles they own so they cannot claim another gateway's events;
    ``include_unowned`` (dispatch owner) covers legacy rows without a stamp.
    """
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if owner_where:
        where.append(owner_where)
        params.extend(owner_params)
    sql = "SELECT * FROM kanban_notify_subs"
    if where:
        sql += " WHERE " + " AND ".join(f"({clause})" for clause in where)
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        if "delivery_metadata" in item:
            item["delivery_metadata"] = _decode_notify_delivery_metadata(item.get("delivery_metadata"))
        out.append(item)
    return out


def count_notify_subs(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
    platform: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> int:
    """Count ``kanban_notify_subs`` rows via a read-only connection — the
    notifier's cheap zero-subscription early exit. Unlike :func:`connect` it
    never creates the file, runs init/migration or opens writable; WAL rows are
    still visible so a fresh sub is never missed. Missing DB / missing table
    counts as zero; platform matches case-insensitively (as notifier routing),
    chat/thread exactly. Raises :class:`sqlite3.Error` if the DB exists but is
    unreadable — callers pick their own fallback.
    """
    path = db_path if db_path is not None else _kb.kanban_db_path(board=board)
    if not path.exists():
        return 0
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    clauses: list[str] = []
    params: list[Any] = []
    if owner_where:
        clauses.append(f"({owner_where})")
        params.extend(owner_params)
    for clause, value in (
        ("LOWER(platform) = LOWER(?)", platform),
        ("chat_id = ?", chat_id),
        ("thread_id = ?", thread_id),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    query = "SELECT COUNT(*) FROM kanban_notify_subs"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(query, params).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    with _kb.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs " + _SUB_KEY_WHERE,
            _sub_key(task_id, platform, chat_id, thread_id),
        )
    return cur.rowcount > 0


def purge_stale_done_notify_subs(conn: sqlite3.Connection, *, max_age_days: int = 30) -> int:
    """Delete notify subs whose task sat in ``done``/``blocked`` untouched for
    longer than ``max_age_days`` (``<= 0`` disables); returns rows deleted.

    Subs survive ``done`` because a reopened task must still notify its origin,
    which accumulates forever on never-archiving boards. ``blocked`` is
    abandoned (unlike ``backlog``/``ready``) so it reaps on the same clock. Age
    = latest event, else ``completed_at``, else ``created_at`` — any activity,
    including a reopen, exempts the sub.

    The notifier keeps subscriptions alive through ``done`` because a completed task can be reopened (review
    corrections, continuation) and the reopened cycle must still notify its origin session. On boards that
    never archive, that retention would otherwise accumulate subscription rows forever — each one scanned
    every notifier tick. This GC bounds that: a task that has been ``done`` with no new events for the
    retention window is treated as settled and its subscriptions are purged. ``blocked`` tasks
    (circuit-breaker trips, dead workers) are reaped on the same clock — they are abandoned, not idle,
    unlike a ``backlog``/``ready`` card that is merely waiting for pickup (#100955).
    """
    try:
        days = int(max_age_days)
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        return 0
    cutoff = int(time.time()) - days * 86400
    with _kb.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id IN ("
            " SELECT t.id FROM tasks t"
            " WHERE t.status IN ('done', 'blocked')"
            " AND COALESCE("
            "  (SELECT MAX(e.created_at) FROM task_events e"
            "   WHERE e.task_id = t.id),"
            "  t.completed_at, t.created_at, 0"
            " ) < ?)",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def _notify_cursor(
    conn: sqlite3.Connection, task_id: str, platform: str, chat_id: str, thread_id: Optional[str],
) -> Optional[int]:
    """``last_event_id`` of one subscription row, or ``None`` when unsubscribed."""
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs " + _SUB_KEY_WHERE,
        _sub_key(task_id, platform, chat_id, thread_id),
    ).fetchone()
    return None if row is None else int(row["last_event_id"])


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` with ``id > last_event_id``. The cursor
    is NOT advanced here; call :func:`advance_notify_cursor` after delivery.
    """
    cursor = _notify_cursor(conn, task_id, platform, chat_id, thread_id)
    if cursor is None:
        return 0, []
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out = [_kb.Event.from_row(r) for r in rows]
    max_id = max([cursor, *(int(r["id"]) for r in rows)])
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``; when events are returned the
    row's ``last_event_id`` has already been advanced inside ``BEGIN IMMEDIATE``,
    so concurrent gateway watchers on the same board DB serialize on SQLite's
    writer lock and only the first claims a given event range. Callers send the
    events, then leave the cursor or call :func:`rewind_notify_cursor` on
    delivery failure.
    """
    with _kb.write_txn(conn):
        old_cursor = _notify_cursor(conn, task_id, platform, chat_id, thread_id)
        if old_cursor is None:
            return 0, 0, []
        new_cursor, events = unseen_events_for_sub(
            conn, task_id=task_id, platform=platform, chat_id=chat_id,
            thread_id=thread_id, kinds=kinds,
        )
        if not events:
            return old_cursor, old_cursor, []
        _cas_cursor(conn, _sub_key(task_id, platform, chat_id, thread_id), new_cursor, old_cursor)
        return old_cursor, new_cursor, events


def _cas_cursor(conn: sqlite3.Connection, key: tuple, new_cursor: int, expected: int) -> sqlite3.Cursor:
    """Move ``last_event_id`` only if it still equals ``expected``."""
    return conn.execute(
        "UPDATE kanban_notify_subs SET last_event_id = ? " + _SUB_KEY_WHERE + " AND last_event_id = ?",
        (int(new_cursor), *key, int(expected)),
    )


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> None:
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? " + _SUB_KEY_WHERE,
            (int(new_cursor), *_sub_key(task_id, platform, chat_id, thread_id)),
        )


def record_notify_ping(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str,
    thread_id: Optional[str] = None, event_id: int,
) -> None:
    """Checkpoint a sent ping independently of the retryable wake cursor."""
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_ping_event_id = MAX(last_ping_event_id, ?) "
            + _SUB_KEY_WHERE,
            (int(event_id), *_sub_key(task_id, platform, chat_id, thread_id)),
        )


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a claim when delivery fails. The CAS guard only rewinds if no later
    notifier advanced the row, so retries never clobber newer progress.
    """
    with _kb.write_txn(conn):
        cur = _cas_cursor(conn, _sub_key(task_id, platform, chat_id, thread_id), old_cursor, claimed_cursor)
    return cur.rowcount > 0


def _update_route_metadata(conn: sqlite3.Connection, key: tuple, mutate) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT delivery_metadata FROM kanban_notify_subs " + _SUB_KEY_WHERE, key).fetchone()
    if row is None:
        return None
    meta = _decode_notify_delivery_metadata(row["delivery_metadata"])
    mutate(meta)
    conn.execute(
        "UPDATE kanban_notify_subs SET delivery_metadata = ? " + _SUB_KEY_WHERE,
        (_encode_notify_delivery_metadata(meta), *key),
    )
    return meta


def bump_route_failures(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str, thread_id: Optional[str] = None,
) -> int:
    """Durably count one more consecutive origin-delivery failure; returns the new count (0 if unsubscribed).

    Durable (not the notifier's in-memory map) so a gateway restart cannot reset the
    bounded-retry budget and postpone the fallback forever.
    """
    def mutate(meta: dict[str, Any]) -> None:
        try:
            meta[ROUTE_FAILURES_KEY] = int(meta.get(ROUTE_FAILURES_KEY) or 0) + 1
        except (TypeError, ValueError):
            meta[ROUTE_FAILURES_KEY] = 1
    with _kb.write_txn(conn):
        meta = _update_route_metadata(conn, _sub_key(task_id, platform, chat_id, thread_id), mutate)
    return int(meta[ROUTE_FAILURES_KEY]) if meta else 0


def clear_route_failures(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str, thread_id: Optional[str] = None,
) -> None:
    with _kb.write_txn(conn):
        _update_route_metadata(conn, _sub_key(task_id, platform, chat_id, thread_id),
                               lambda meta: meta.pop(ROUTE_FAILURES_KEY, None))


def ensure_fallback_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    start_cursor: int,
    origin: str,
    cause: Optional[str] = None,
) -> bool:
    """Create the task's single notify-only fallback row; False when one already exists.

    Dedup is per task, inside one write transaction: a task never gets a second
    fallback, whatever destination or how many ticks/events. When the destination
    already holds a (silent mirror) row for the same key, that row is converted in
    place — its cursor is moved to ``start_cursor`` so the first unacknowledged
    actionable event is delivered, and its mode is forced to ``notify`` so the
    fallback never wakes a conversation. ``origin`` (``platform:chat[:thread]``) is
    recorded so the ping names the originating conversation. ``cause`` distinguishes
    a stale Desktop update from an actual delivery failure without changing the schema.
    """
    key = _sub_key(task_id, platform, chat_id, thread_id)
    start = int(start_cursor)
    with _kb.write_txn(conn):
        sent = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
            (task_id, FALLBACK_SENT_EVENT),
        ).fetchone()
        if sent is not None:
            return False
        rows = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,)).fetchall()
        if any(_decode_notify_delivery_metadata(r["delivery_metadata"]).get(ROUTE_ROLE_KEY) == ROLE_FALLBACK
               for r in rows):
            return False
        existing = next((r for r in rows if (r["platform"], r["chat_id"], r["thread_id"] or "") == key[1:]), None)
        meta = _decode_notify_delivery_metadata(existing["delivery_metadata"]) if existing is not None else {}
        meta.pop(ROUTE_FAILURES_KEY, None)
        meta.update({ROUTE_ROLE_KEY: ROLE_FALLBACK, ROUTE_ORIGIN_KEY: str(origin)[:200]})
        if cause:
            meta[ROUTE_FALLBACK_CAUSE_KEY] = str(cause)[:80]
        metadata_json = _encode_notify_delivery_metadata(meta)
        if existing is None:
            conn.execute(
                """
                INSERT INTO kanban_notify_subs
                    (task_id, platform, chat_id, thread_id, user_id, chat_type, notifier_profile,
                     delivery_mode, delivery_metadata, created_at, last_event_id, last_ping_event_id)
                VALUES (?, ?, ?, ?, ?, 'dm', ?, 'notify', ?, ?, ?, ?)
                """,
                (*key, user_id, notifier_profile, metadata_json, int(time.time()), start, start),
            )
        else:
            conn.execute(
                "UPDATE kanban_notify_subs SET delivery_mode = 'notify', delivery_metadata = ?, "
                "last_event_id = ? " + _SUB_KEY_WHERE,
                (metadata_json, start, *key),
            )
    return True


def record_fallback_sent(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    """Persist the one-shot fallback delivery marker independently of its subscription row."""
    with _kb.write_txn(conn):
        existing = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
            (task_id, FALLBACK_SENT_EVENT),
        ).fetchone()
        if existing is not None:
            return False
        _kb._append_event(conn, task_id, FALLBACK_SENT_EVENT, {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id or "",
        })
    return True


def ensure_topic_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    role: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    start_cursor: int,
    origin: str,
) -> bool:
    """Create one durable notify-only Ops-topic route for ``task_id``/``role``.

    The role is the dedup identity, while the subscription primary key remains
    the physical Telegram destination. Existing rows are never repurposed: in
    particular, a fallback already occupying Alerts remains the task's single
    alert route. New rows begin immediately before the event that selected the
    route, so activation cannot replay older card history.
    """
    if role not in (ROLE_ALERTS, ROLE_APPROVALS, ROLE_OPS):
        raise ValueError(f"unsupported topic route role: {role}")
    key = _sub_key(task_id, platform, chat_id, thread_id)
    start = int(start_cursor)
    with _kb.write_txn(conn):
        rows = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,)).fetchall()
        for row in rows:
            meta = _decode_notify_delivery_metadata(row["delivery_metadata"])
            if meta.get(ROUTE_ROLE_KEY) == role:
                return False
            if (row["platform"], row["chat_id"], row["thread_id"] or "") == key[1:]:
                return False
        metadata_json = _encode_notify_delivery_metadata({
            ROUTE_ROLE_KEY: role,
            ROUTE_ORIGIN_KEY: str(origin)[:200],
        })
        conn.execute(
            """
            INSERT INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, chat_type, notifier_profile,
                 delivery_mode, delivery_metadata, created_at, last_event_id, last_ping_event_id)
            VALUES (?, ?, ?, ?, 'group', ?, 'notify', ?, ?, ?, ?)
            """,
            (*key, notifier_profile, metadata_json, int(time.time()), start, start),
        )
    return True


def unconsumed_events_for_platform(
    conn: sqlite3.Connection, *, platform: str, kinds: Iterable[str], min_created_at: int, max_created_at: int,
) -> list[tuple[dict, Event]]:
    """``(sub, event)`` pairs whose event the ``platform`` subscription has not claimed yet,
    restricted to a ``created_at`` window. Used to detect a Desktop (``tui``) origin that
    left an actionable event unacknowledged past the staleness window."""
    kind_list = list(kinds)
    if not kind_list:
        return []
    rows = conn.execute(
        "SELECT s.task_id AS s_task_id, s.platform AS s_platform, s.chat_id AS s_chat_id, "
        "       s.thread_id AS s_thread_id, s.last_event_id AS s_cursor, s.delivery_metadata AS s_meta, "
        "       s.notifier_profile AS s_profile, e.* "
        "  FROM kanban_notify_subs s JOIN task_events e "
        "    ON e.task_id = s.task_id AND e.id > s.last_event_id "
        " WHERE LOWER(s.platform) = LOWER(?) AND e.created_at >= ? AND e.created_at <= ? "
        "   AND e.kind IN (" + ",".join("?" * len(kind_list)) + ") "
        " ORDER BY e.id ASC",
        (platform, int(min_created_at), int(max_created_at), *kind_list),
    ).fetchall()
    out: list[tuple[dict, Event]] = []
    for r in rows:
        sub = {
            "task_id": r["s_task_id"], "platform": r["s_platform"], "chat_id": r["s_chat_id"],
            "thread_id": r["s_thread_id"] or "", "last_event_id": int(r["s_cursor"]),
            "notifier_profile": r["s_profile"],
            "delivery_metadata": _decode_notify_delivery_metadata(r["s_meta"]),
        }
        out.append((sub, _kb.Event.from_row(r)))
    return out


def claim_notify_cursor_range(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str, thread_id: Optional[str] = None,
    old_cursor: int, new_cursor: int,
) -> bool:
    """CAS-advance a cursor over a caller-selected prefix of unseen events (False if another notifier won)."""
    with _kb.write_txn(conn):
        cur = _cas_cursor(conn, _sub_key(task_id, platform, chat_id, thread_id), new_cursor, old_cursor)
    return cur.rowcount > 0


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
