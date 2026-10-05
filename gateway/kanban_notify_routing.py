"""Origin-first Kanban notification routing policy.

Opt-in via ``kanban.notification_routing: origin_first`` (default ``legacy`` keeps the
historical behaviour exactly: every subscription row delivers its own events).

Under ``origin_first``:

* **Origin is primary.** The exact conversation that created the card — a Desktop
  session (``platform="tui"``), a Telegram DM, or a Telegram forum topic — receives
  actionable events and, if its mode asks for it, is woken.
* **No blanket mirroring.** When a card has an origin, every other unstamped row is a
  *mirror* and stays silent (its cursor still advances so nothing replays later). This
  neutralises the cron-written home-DM ``notify+wake`` rows without touching them.
* **Bounded durable retry.** A failing push origin rewinds and retries every tick; the
  consecutive-failure count is stored in the row so restarts cannot reset it.
* **Exactly one fallback.** After ``MAX_SEND_FAILURES`` consecutive origin failures —
  or a Desktop origin leaving an actionable event unclaimed for longer than
  ``kanban.origin_stale_seconds`` (default 600s) — one notify-only row is created in the
  configured Gohan Ops Alerts topic. It never wakes a conversation and names the card and
  the unreachable origin. Dedup is per task; the Telegram home DM is never a fallback.
* **Internal events are silent.** Heartbeats, claims, spawns, promotions, dependency
  waits, routine status/review churn and auto-retried crashes/timeouts never reach Josh;
  only completion, real blocks (incl. ``status→blocked``), give-ups and triage escalations do.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from hermes_cli.kanban_db_notify import (
    ROLE_ALERTS,
    ROLE_APPROVALS,
    ROLE_FALLBACK,
    ROLE_MIRROR,
    ROLE_OPS,
    ROLE_ORIGIN,
    ROUTE_FAILURES_KEY,
    ROUTE_ORIGIN_KEY,
    ROUTE_ROLE_KEY,
)

__all__ = [
    "ROUTE_ROLE_KEY", "ROLE_ORIGIN", "ROLE_MIRROR", "ROLE_FALLBACK", "ROLE_PASSIVE",
    "ROLE_ALERTS", "ROLE_APPROVALS", "ROLE_OPS", "ORIGIN_FIRST", "LEGACY",
    "ACTIONABLE_KINDS", "DEFAULT_ORIGIN_STALE_SECONDS", "routing_mode", "origin_stale_seconds",
    "is_actionable", "event_topic_role", "sub_role", "describe_origin", "strip_route_metadata",
    "fallback_prefix", "topic_prefix",
]

LEGACY = "legacy"
ORIGIN_FIRST = "origin_first"
_MODES = (LEGACY, ORIGIN_FIRST)
DEFAULT_ORIGIN_STALE_SECONDS = 600
# Look back at most this far for unacknowledged Desktop events: older ones predate
# the activation (or a long-closed session) and must not burst into Telegram at once.
STALE_LOOKBACK_SECONDS = 2 * 86400

# Events that need Josh/orchestrator attention. ``status`` and ``block_loop_detected``
# are filtered further by ``is_actionable``.
ACTIONABLE_KINDS = ("completed", "blocked", "gave_up", "block_loop_detected", "status")
_ACTIONABLE_STATUSES = {"blocked", "triage"}


def _kanban_cfg(cfg: Any) -> dict:
    if not isinstance(cfg, dict):
        return {}
    kanban = cfg.get("kanban")
    return kanban if isinstance(kanban, dict) else {}


def routing_mode(cfg: Any) -> str:
    value = str(_kanban_cfg(cfg).get("notification_routing") or LEGACY).strip().lower().replace("-", "_")
    return value if value in _MODES else LEGACY


def origin_stale_seconds(cfg: Any) -> int:
    try:
        value = int(_kanban_cfg(cfg).get("origin_stale_seconds", DEFAULT_ORIGIN_STALE_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_ORIGIN_STALE_SECONDS
    return max(60, value)


def is_actionable(ev: Any) -> bool:
    kind = getattr(ev, "kind", "")
    payload = getattr(ev, "payload", None) or {}
    if kind == "status":
        return payload.get("status") in _ACTIONABLE_STATUSES
    if kind == "blocked" and payload.get("kind") == "dependency":
        return False  # waits in todo and resumes on its own when the parent finishes
    return kind in ACTIONABLE_KINDS


def event_topic_role(ev: Any) -> Optional[str]:
    """Dedicated Ops topic for an actionable event with a non-Telegram origin."""
    payload = getattr(ev, "payload", None) or {}
    if ev.kind in {"gave_up", "block_loop_detected"}:
        return ROLE_ALERTS
    if ev.kind == "blocked":
        kind = payload.get("kind")
        if kind == "needs_input":
            return ROLE_APPROVALS
        if kind in {"capability", "transient"}:
            return ROLE_ALERTS
    return None


def _meta(sub: dict) -> dict:
    meta = sub.get("delivery_metadata")
    return meta if isinstance(meta, dict) else {}


def _stamped_role(sub: dict) -> Optional[str]:
    role = _meta(sub).get(ROUTE_ROLE_KEY)
    return role if role in (
        ROLE_ORIGIN, ROLE_MIRROR, ROLE_FALLBACK, ROLE_ALERTS, ROLE_APPROVALS, ROLE_OPS,
    ) else None


# Not persisted: an unstamped non-Desktop row on a card with no origin at all. It keeps
# visibility for headless cards but delivers notify-only — it is not anyone's conversation,
# so it never wakes. Genuine conversation origins are stamped by the creator path.
ROLE_PASSIVE = "passive"


def sub_role(sub: dict, siblings: Iterable[dict]) -> str:
    """Classify one row against all rows of the same task.

    Stamped rows keep their stamp. For unstamped (pre-activation) rows: when a stamped
    origin exists every other row is a mirror; otherwise a Desktop (``tui``) row is the
    origin and other rows mirror it. With no Desktop row, every unstamped non-Desktop row
    is passive; only an explicit ``route_role=origin`` stamp proves that it is a genuine
    conversation rather than a retired cron/home-DM mirror with incidental metadata.
    """
    stamped = _stamped_role(sub)
    if stamped:
        return stamped
    rows = list(siblings)
    if any(_stamped_role(r) == ROLE_ORIGIN for r in rows):
        return ROLE_MIRROR
    if (sub.get("platform") or "").lower() == "tui":
        return ROLE_ORIGIN
    if any((r.get("platform") or "").lower() == "tui" for r in rows):
        return ROLE_MIRROR
    return ROLE_PASSIVE


def describe_origin(sub: dict) -> str:
    platform = (sub.get("platform") or "?").lower()
    label = "desktop session tui" if platform == "tui" else platform
    thread = sub.get("thread_id") or ""
    return f"{label}:{sub.get('chat_id') or '?'}" + (f":{thread}" if thread else "")


def strip_route_metadata(metadata: dict) -> dict:
    return {k: v for k, v in metadata.items() if k not in (ROUTE_ROLE_KEY, ROUTE_FAILURES_KEY, ROUTE_ORIGIN_KEY)}


def fallback_prefix(sub: dict) -> str:
    origin = _meta(sub).get(ROUTE_ORIGIN_KEY)
    return f"↪ Fallback — origin unreachable ({origin}). " if origin else "↪ Fallback — origin unreachable. "


def topic_prefix(sub: dict) -> str:
    origin = _meta(sub).get(ROUTE_ORIGIN_KEY)
    return f"Origin: {origin}. " if origin else ""


def stale_window(cfg: Any, now: Optional[float] = None) -> tuple[int, int]:
    now = int(now if now is not None else time.time())
    return now - STALE_LOOKBACK_SECONDS, now - origin_stale_seconds(cfg)
