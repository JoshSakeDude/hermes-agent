"""Origin-first Kanban notification routing (``kanban.notification_routing: origin_first``).

Policy under test:
- the exact originating conversation (Desktop session, Telegram DM, Telegram topic) is primary;
- unstamped secondary rows on a card that has an origin are mirrors and stay silent
  (no blanket Telegram mirroring of Desktop-origin cards);
- origin delivery failures rewind and retry; after a bounded number of consecutive failures
  (or a Desktop origin leaving an actionable event unclaimed past the staleness window) exactly
  one notify-only Telegram fallback row is created in the Gohan Ops Alerts topic;
- the fallback never wakes a conversation and carries the card/origin identity;
- internal / non-actionable lifecycle events never reach Josh.
"""

import asyncio
import json
import time

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.kanban_notify_routing import ROUTE_ROLE_KEY
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway.server import _collect_kanban_notifications

HOME_CHAT = "home-chat"
ORIGIN_CHAT = "origin-chat"
DESKTOP_KEY = "desktop-session-key-1"
OPS_CHAT = "ops-forum"
TOPICS = {"tasks": "11", "approvals": "12", "ops": "13", "alerts": "14"}


class RecordingAdapter:
    def __init__(self, fail_chats=()):
        self.sent = []
        self.handled = []
        self.fail_chats = set(fail_chats)
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        if chat_id in self.fail_chats:
            raise RuntimeError("chat not found")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def _make_runner(adapter, *, home=True, started_at=0):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Most tests build a fresh board before constructing the runner. Let them opt
    # out of the production start guard; backlog regressions pass a real timestamp.
    runner._kanban_notify_started_at = started_at
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    platform_cfg = PlatformConfig(enabled=True)
    if home:
        platform_cfg.home_channel = HomeChannel(platform=Platform.TELEGRAM, chat_id=HOME_CHAT, name="Josh")
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: platform_cfg})
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _tick(monkeypatch, runner, n=1):
    for _ in range(n):
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "origin-first.db"))
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "telegram_topics.json").write_text(
        json.dumps({"chat_id": OPS_CHAT, "topics": TOPICS}), encoding="utf-8")
    kb.init_db()
    # The watcher reads the live config once; pin the mode under test instead.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"notification_routing": "origin_first"}})
    return tmp_path


def _task(**kw):
    conn = kbc.connect()
    try:
        return kb.create_task(conn, title=kw.pop("title", "origin card"), assignee="gohanlite", **kw)
    finally:
        conn.close()


def _sub(tid, **kw):
    conn = kbc.connect()
    try:
        kbn.add_notify_sub(conn, task_id=tid, **kw)
    finally:
        conn.close()


def _origin_meta(**extra):
    return {ROUTE_ROLE_KEY: "origin", **extra}


def _desktop_origin(tid):
    _sub(tid, platform="tui", chat_id=DESKTOP_KEY, notifier_profile="default",
         delivery_metadata=_origin_meta())


def _blanket_mirror(tid):
    # Exactly what ~/.hermes/scripts/kanban_wake_subscribe.py writes today.
    _sub(tid, platform="telegram", chat_id=HOME_CHAT, user_id=HOME_CHAT, chat_type="dm",
         notifier_profile="default", delivery_mode="notify+wake")


def _complete(tid, summary="shipped"):
    conn = kbc.connect()
    try:
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def _archive(tid):
    conn = kbc.connect()
    try:
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()


def _event(tid, kind, payload=None):
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind, payload)
    finally:
        conn.close()


def _age_events(tid, seconds):
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ?", (seconds, tid))
    finally:
        conn.close()


def _set_event_created_at(tid, created_at):
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (created_at, tid))
    finally:
        conn.close()


def _subs(tid):
    conn = kbc.connect()
    try:
        return kbn.list_notify_subs(conn, tid)
    finally:
        conn.close()


def _fallback_rows(tid):
    return [s for s in _subs(tid) if (s.get("delivery_metadata") or {}).get(ROUTE_ROLE_KEY) == "fallback"]


def _topic_rows(tid, role):
    return [s for s in _subs(tid) if (s.get("delivery_metadata") or {}).get(ROUTE_ROLE_KEY) == role]


def _sent_destinations(adapter):
    return [(s["chat_id"], s["metadata"].get("thread_id")) for s in adapter.sent]


# --- Desktop origin ---------------------------------------------------------


def test_desktop_origin_success_does_not_mirror_or_fallback(board, monkeypatch):
    tid = _task()
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _complete(tid)

    texts = _collect_kanban_notifications({"session_key": DESKTOP_KEY})
    assert len(texts) == 1 and tid in texts[0]

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    _tick(monkeypatch, runner, n=2)

    assert adapter.sent == []
    assert adapter.handled == []  # the primary Telegram conversation is never woken
    assert _fallback_rows(tid) == []


def test_fresh_unclaimed_desktop_event_is_not_mirrored(board, monkeypatch):
    """Desktop closed briefly: inside the staleness window Telegram stays quiet."""
    tid = _task()
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _complete(tid)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter))

    assert adapter.sent == [] and adapter.handled == []
    assert _fallback_rows(tid) == []


def test_legacy_tui_row_counts_as_origin_for_existing_cards(board, monkeypatch):
    """Rows written before stamping: a tui row is the Desktop origin, the unstamped Telegram row a mirror."""
    tid = _task()
    _sub(tid, platform="tui", chat_id=DESKTOP_KEY, notifier_profile="default")
    _blanket_mirror(tid)
    _complete(tid)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter))
    assert adapter.sent == [] and adapter.handled == []


def test_stale_desktop_origin_creates_one_notify_only_fallback(board, monkeypatch):
    tid = _task(title="desktop card")
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _complete(tid)
    _age_events(tid, 3600)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    _tick(monkeypatch, runner, n=3)

    fallbacks = _fallback_rows(tid)
    assert len(fallbacks) == 1
    assert fallbacks[0]["platform"] == "telegram" and fallbacks[0]["chat_id"] == OPS_CHAT
    assert fallbacks[0]["thread_id"] == TOPICS["alerts"]
    assert fallbacks[0]["delivery_mode"] == "notify"
    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert tid in text and "desktop card" in text
    assert "tui" in text and DESKTOP_KEY in text  # original card/session identity
    assert ROUTE_ROLE_KEY not in adapter.sent[0]["metadata"]
    assert adapter.handled == []  # fallback never wakes a conversation


def test_stale_desktop_completions_share_one_alerts_digest(board, monkeypatch):
    task_ids = []
    for title in ("first desktop card", "second desktop card"):
        tid = _task(title=title)
        task_ids.append(tid)
        _desktop_origin(tid)
        _complete(tid, summary=f"finished {title}")
        _age_events(tid, 3600)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["alerts"])]
    text = adapter.sent[0]["text"]
    assert all(tid in text for tid in task_ids)
    assert "first desktop card" in text and "second desktop card" in text
    assert "Desktop update unread" in text
    assert "origin unreachable" not in text


def test_urgent_alert_bypasses_digest_while_completions_wait_ten_minutes(board, monkeypatch):
    now = [10_000]
    monkeypatch.setattr("gateway.kanban_notify_routing.time.time", lambda: now[0])
    monkeypatch.setattr("gateway.kanban_watchers_notifier.time.time", lambda: now[0])
    completion_ids = []
    for title in ("timed first", "timed second"):
        tid = _task(title=title)
        completion_ids.append(tid)
        _desktop_origin(tid)
        _complete(tid)
        _set_event_created_at(tid, 9_400)
    urgent = _task(title="blocked now")
    _desktop_origin(urgent)
    _event(urgent, "blocked", {"kind": "capability", "reason": "credentials missing"})

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    now[0] = 9_999
    _tick(monkeypatch, runner, n=2)

    assert len(adapter.sent) == 1
    assert urgent in adapter.sent[0]["text"]
    assert all(tid not in adapter.sent[0]["text"] for tid in completion_ids)
    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["alerts"])]

    now[0] = 10_000
    _tick(monkeypatch, runner, n=2)

    assert len(adapter.sent) == 2
    digest = adapter.sent[1]["text"]
    assert all(tid in digest for tid in completion_ids)
    assert "Desktop update unread" in digest
    assert "origin unreachable" not in digest
    assert _sent_destinations(adapter)[1] == (OPS_CHAT, TOPICS["alerts"])


def test_desktop_completion_digest_is_bounded(board, monkeypatch):
    from gateway.kanban_watchers_notifier import MAX_FALLBACK_DIGEST_ITEMS

    task_ids = []
    for index in range(MAX_FALLBACK_DIGEST_ITEMS + 1):
        tid = _task(title=f"bounded card {index}")
        task_ids.append(tid)
        _desktop_origin(tid)
        _complete(tid)
        _age_events(tid, 3600)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert len(adapter.sent) == 2
    assert all(
        (sent["chat_id"], sent["metadata"].get("thread_id")) == (OPS_CHAT, TOPICS["alerts"])
        for sent in adapter.sent
    )
    combined = "\n".join(sent["text"] for sent in adapter.sent)
    assert all(tid in combined for tid in task_ids)


def test_archived_stale_desktop_origin_never_loops_fallback(board, monkeypatch):
    """An archived card cannot recreate its fallback after archive delivery removes that row."""
    tid = _task(title="archived desktop card")
    _desktop_origin(tid)
    _complete(tid)
    _archive(tid)
    _age_events(tid, 3600)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=4)

    assert len(adapter.sent) <= 1
    assert adapter.handled == []
    assert _subs(tid) == []


def test_existing_notify_wake_mirror_converts_to_fallback_that_never_wakes(board, monkeypatch):
    """Fallback role is the final wake guard, even if a stale writer restores the old mirror mode."""
    tid = _task(title="converted mirror")
    _blanket_mirror(tid)

    conn = kbc.connect()
    try:
        assert kbn.ensure_fallback_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=HOME_CHAT,
            user_id=HOME_CHAT, notifier_profile="default", start_cursor=0,
            origin=f"tui:{DESKTOP_KEY}",
        ) is True
        # Conversion normally forces notify-only. A concurrent legacy cron can
        # re-subscribe the same key as notify+wake, so role must remain the
        # defense-in-depth guarantee that fallback never wakes a conversation.
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=HOME_CHAT,
            user_id=HOME_CHAT, chat_type="dm", notifier_profile="default",
            delivery_mode="notify+wake",
        )
        kb._append_event(conn, tid, "blocked", {"reason": "needs Josh", "kind": "needs_input"})
    finally:
        conn.close()

    (fallback,) = _fallback_rows(tid)
    assert fallback["delivery_mode"] == "notify+wake"

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert adapter.handled == []


def test_originless_passive_cron_row_routes_to_ops_and_never_wakes_fred(board, monkeypatch):
    """A bare cron-written Fred row is a silent mirror; headless work routes to Ops."""
    tid = _task(title="headless cron card")
    _blanket_mirror(tid)  # no routing metadata: ROLE_PASSIVE when no origin exists
    _event(tid, "blocked", {"reason": "needs Josh", "kind": "needs_input"})

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["ops"])]
    assert tid in adapter.sent[0]["text"]
    assert adapter.handled == []
    assert HOME_CHAT not in [s["chat_id"] for s in adapter.sent]


def test_unstamped_home_dm_metadata_is_not_an_origin(board, monkeypatch):
    """Incidental metadata on a retired Fred row cannot turn it into an origin."""
    tid = _task(title="metadata-only cron card")
    _sub(tid, platform="telegram", chat_id=HOME_CHAT, user_id=HOME_CHAT, chat_type="dm",
         notifier_profile="default", delivery_mode="notify+wake",
         delivery_metadata={"chat_type": "dm"})
    _complete(tid)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["ops"])]
    assert HOME_CHAT not in [s["chat_id"] for s in adapter.sent]


def test_activation_ignores_subscription_backlog_but_routes_new_events(board, monkeypatch):
    """A fresh notifier must not replay months of unseen Desktop-origin events."""
    failure = _task(title="old desktop failure")
    _desktop_origin(failure)
    _event(failure, "gave_up", {"failures": 2, "error": "old boom"})
    decision = _task(title="old desktop decision")
    _desktop_origin(decision)
    _event(decision, "blocked", {"kind": "needs_input", "reason": "old choice"})
    _age_events(failure, 30 * 86400)
    _age_events(decision, 30 * 86400)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, started_at=int(time.time()))
    _tick(monkeypatch, runner, n=2)

    assert adapter.sent == []
    assert _topic_rows(failure, "alerts") == []
    assert _topic_rows(decision, "approvals") == []

    _event(failure, "gave_up", {"failures": 3, "error": "new boom"})
    _event(decision, "blocked", {"kind": "needs_input", "reason": "new choice"})
    _tick(monkeypatch, runner, n=2)

    assert sorted(_sent_destinations(adapter)) == sorted([
        (OPS_CHAT, TOPICS["alerts"]),
        (OPS_CHAT, TOPICS["approvals"]),
    ])
    assert len(_topic_rows(failure, "alerts")) == 1
    assert len(_topic_rows(decision, "approvals")) == 1


@pytest.mark.parametrize("kind,payload", [
    ("gave_up", {"failures": 2, "error": "boom"}),
    ("block_loop_detected", {"kind": "transient", "reason": "again"}),
    ("blocked", {"kind": "capability", "reason": "no access"}),
    ("blocked", {"kind": "transient", "reason": "network"}),
])
def test_desktop_origin_failures_route_once_to_alerts_never_fred(board, monkeypatch, kind, payload):
    tid = _task(title="desktop failure")
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _event(tid, kind, payload)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=3)

    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["alerts"])]
    assert len(_topic_rows(tid, "alerts")) == 1
    assert tid in adapter.sent[0]["text"] and "tui" in adapter.sent[0]["text"]
    assert adapter.handled == []


def test_desktop_origin_needs_input_routes_once_to_approvals_never_fred(board, monkeypatch):
    tid = _task(title="desktop decision")
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "pick one"})

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=3)

    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["approvals"])]
    assert len(_topic_rows(tid, "approvals")) == 1
    assert tid in adapter.sent[0]["text"] and "tui" in adapter.sent[0]["text"]
    assert adapter.handled == []


def test_telegram_origin_failure_has_no_duplicate_alert_notice(board, monkeypatch):
    tid = _task(session_id="agent:main:telegram:dm:origin-chat")
    _sub(tid, platform="telegram", chat_id=ORIGIN_CHAT, user_id=ORIGIN_CHAT, chat_type="dm",
         notifier_profile="default", delivery_mode="notify+wake",
         delivery_metadata=_origin_meta(chat_type="dm"))
    _event(tid, "gave_up", {"failures": 2, "error": "boom"})

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert [s["chat_id"] for s in adapter.sent] == [ORIGIN_CHAT]
    assert _topic_rows(tid, "alerts") == []


def test_originless_completed_routes_to_ops_not_fred(board, monkeypatch):
    tid = _task(title="headless complete")
    _blanket_mirror(tid)
    _complete(tid)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["ops"])]
    assert len(_topic_rows(tid, "ops")) == 1
    assert adapter.handled == []


def test_originless_card_without_any_subscription_routes_to_ops(board, monkeypatch):
    tid = _task(title="cli card")
    _complete(tid)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert _sent_destinations(adapter) == [(OPS_CHAT, TOPICS["ops"])]
    assert len(_topic_rows(tid, "ops")) == 1


# --- Telegram origins -------------------------------------------------------


def test_telegram_dm_origin_delivers_and_wakes_exact_dm(board, monkeypatch):
    tid = _task(session_id="agent:main:telegram:dm:origin-chat")
    _sub(tid, platform="telegram", chat_id=ORIGIN_CHAT, user_id=ORIGIN_CHAT, chat_type="dm",
         notifier_profile="default", delivery_mode="notify+wake",
         delivery_metadata=_origin_meta(chat_type="dm"))
    _complete(tid)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)

    assert [s["chat_id"] for s in adapter.sent] == [ORIGIN_CHAT]
    assert ROUTE_ROLE_KEY not in adapter.sent[0]["metadata"]
    assert len(adapter.handled) == 1
    src = adapter.handled[0].source
    assert src.chat_id == ORIGIN_CHAT and src.chat_type == "dm"
    assert _fallback_rows(tid) == []


def test_telegram_topic_origin_delivers_to_exact_topic(board, monkeypatch):
    tid = _task()
    _sub(tid, platform="telegram", chat_id="-100forum", thread_id="5", chat_type="group",
         notifier_profile="default", delivery_mode="notify+wake",
         delivery_metadata=_origin_meta(chat_type="group", thread_id="5"))
    _blanket_mirror(tid)
    _complete(tid)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter))

    assert [(s["chat_id"], s["metadata"].get("thread_id")) for s in adapter.sent] == [("-100forum", "5")]
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.thread_id == "5"
    assert adapter.handled[0].source.chat_id == "-100forum"


# --- Retry / fallback / dedupe ---------------------------------------------


def _failing_topic_origin(tid):
    _sub(tid, platform="telegram", chat_id=ORIGIN_CHAT, thread_id="9", chat_type="group",
         notifier_profile="default", delivery_mode="notify+wake",
         delivery_metadata=_origin_meta(chat_type="group", thread_id="9"))


def test_origin_retries_without_fallback_then_delivers_once(board, monkeypatch):
    tid = _task()
    _failing_topic_origin(tid)
    _complete(tid)

    adapter = RecordingAdapter(fail_chats={ORIGIN_CHAT})
    runner = _make_runner(adapter)
    _tick(monkeypatch, runner, n=3)
    assert adapter.sent == [] and _fallback_rows(tid) == []
    # Failure count is durable (survives a gateway restart / fresh in-memory counters).
    origin = [s for s in _subs(tid) if s["chat_id"] == ORIGIN_CHAT][0]
    assert origin["delivery_metadata"]["route_failures"] == 3

    adapter.fail_chats.clear()
    _tick(monkeypatch, _make_runner(adapter), n=2)
    assert [s["chat_id"] for s in adapter.sent] == [ORIGIN_CHAT]
    assert _fallback_rows(tid) == []
    origin = [s for s in _subs(tid) if s["chat_id"] == ORIGIN_CHAT][0]
    assert "route_failures" not in origin["delivery_metadata"]


def test_permanent_origin_failure_creates_single_fallback(board, monkeypatch):
    from gateway.kanban_watchers_notifier import MAX_SEND_FAILURES

    tid = _task(title="topic card")
    _failing_topic_origin(tid)
    _complete(tid)

    adapter = RecordingAdapter(fail_chats={ORIGIN_CHAT})
    runner = _make_runner(adapter)
    _tick(monkeypatch, runner, n=MAX_SEND_FAILURES - 1)
    assert adapter.sent == [] and _fallback_rows(tid) == []

    _tick(monkeypatch, runner, n=4)
    fallbacks = _fallback_rows(tid)
    assert len(fallbacks) == 1 and fallbacks[0]["chat_id"] == OPS_CHAT
    assert fallbacks[0]["thread_id"] == TOPICS["alerts"]
    assert [s["chat_id"] for s in adapter.sent] == [OPS_CHAT]
    text = adapter.sent[0]["text"]
    assert tid in text and "topic card" in text and ORIGIN_CHAT in text
    assert "origin unreachable" in text
    assert "Desktop update unread" not in text
    assert adapter.handled == []
    # The dead origin row is retired so it stops spinning.
    assert not [s for s in _subs(tid) if s["chat_id"] == ORIGIN_CHAT]


def test_ensure_fallback_second_call_is_noop_and_preserves_progress(board):
    """Per-task fallback dedup must not rewind a delivered row or rewrite its route identity."""
    tid = _task()
    _blanket_mirror(tid)
    _event(tid, "blocked", {"reason": "first", "kind": "needs_input"})

    conn = kbc.connect()
    try:
        event_id = int(conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,),
        ).fetchone()[0])
        assert kbn.ensure_fallback_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=HOME_CHAT,
            user_id=HOME_CHAT, notifier_profile="default", start_cursor=event_id - 1,
            origin=f"tui:{DESKTOP_KEY}",
        ) is True
        kbn.advance_notify_cursor(
            conn, task_id=tid, platform="telegram", chat_id=HOME_CHAT,
            new_cursor=event_id,
        )
        kbn.record_notify_ping(
            conn, task_id=tid, platform="telegram", chat_id=HOME_CHAT,
            event_id=event_id,
        )
        before = kbn.list_notify_subs(conn, tid)[0]

        assert kbn.ensure_fallback_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=HOME_CHAT,
            user_id="different-user", notifier_profile="different-profile",
            start_cursor=0, origin="telegram:other-origin",
        ) is False
        after = kbn.list_notify_subs(conn, tid)[0]
    finally:
        conn.close()

    assert after == before
    assert after["last_event_id"] == event_id
    assert after["last_ping_event_id"] == event_id
    assert after["delivery_metadata"] == before["delivery_metadata"]


def test_fallback_sent_marker_deduplicates_after_row_deletion(board):
    tid = _task()
    conn = kbc.connect()
    try:
        assert kbn.ensure_fallback_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=OPS_CHAT,
            thread_id=TOPICS["alerts"], notifier_profile="default",
            start_cursor=0, origin=f"tui:{DESKTOP_KEY}",
        ) is True
        assert kbn.record_fallback_sent(
            conn, task_id=tid, platform="telegram", chat_id=OPS_CHAT,
            thread_id=TOPICS["alerts"],
        ) is True
        assert kbn.remove_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=OPS_CHAT,
            thread_id=TOPICS["alerts"],
        ) is True

        assert kbn.ensure_fallback_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=OPS_CHAT,
            thread_id=TOPICS["alerts"], notifier_profile="default",
            start_cursor=0, origin=f"tui:{DESKTOP_KEY}",
        ) is False
    finally:
        conn.close()

    assert _fallback_rows(tid) == []


def test_later_desktop_stale_scan_does_not_rewind_or_redeliver_fallback(board, monkeypatch):
    """The stale scan keeps seeing the unclaimed Desktop event, but fallback creation is one-shot."""
    tid = _task(title="stale scan dedup")
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _complete(tid)
    _age_events(tid, 3600)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    _tick(monkeypatch, runner)
    assert len(adapter.sent) == 1
    before = _fallback_rows(tid)[0]

    real_ensure = kbn.ensure_fallback_notify_sub
    calls = []

    def recording_ensure(conn, **kwargs):
        row_before = kbn.list_notify_subs(conn, tid)
        result = real_ensure(conn, **kwargs)
        row_after = kbn.list_notify_subs(conn, tid)
        calls.append((result, row_before, row_after))
        return result

    monkeypatch.setattr(kbn, "ensure_fallback_notify_sub", recording_ensure)
    _tick(monkeypatch, runner)

    assert calls, "later stale scan must exercise the existing-fallback guard"
    assert all(result is False and rows_before == rows_after
               for result, rows_before, rows_after in calls)
    assert _fallback_rows(tid)[0] == before
    assert len(adapter.sent) == 1
    assert adapter.handled == []


def test_fallback_is_deduplicated_across_ticks_and_events(board, monkeypatch):
    tid = _task()
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _complete(tid, summary="first")
    _age_events(tid, 3600)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    _tick(monkeypatch, runner, n=3)
    assert len(_fallback_rows(tid)) == 1
    assert len(adapter.sent) == 1

    _event(tid, "status", {"status": "blocked"})
    _age_events(tid, 3600)
    _tick(monkeypatch, runner, n=3)
    assert len(_fallback_rows(tid)) == 1
    assert len(adapter.sent) == 2  # one ping per actionable event, never duplicated
    assert adapter.handled == []


def test_missing_topic_map_warns_and_never_invents_dm_fallback(board, monkeypatch, caplog):
    from hermes_constants import get_default_hermes_root

    (get_default_hermes_root() / "telegram_topics.json").unlink()
    tid = _task()
    _desktop_origin(tid)
    _complete(tid)
    _age_events(tid, 3600)

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)
    assert adapter.sent == [] and _fallback_rows(tid) == []
    assert "telegram topic map" in caplog.text.lower()


# --- Internal event silence -------------------------------------------------


INTERNAL = [
    ("heartbeat", {"note": "alive"}),
    ("claimed", {"run_id": 1}),
    ("spawned", {"pid": 1}),
    ("promoted", None),
    ("dependency_wait", {"parent": "t_x"}),
    ("status", {"status": "ready"}),
    ("status", {"status": "review"}),
    ("review_requested", {"summary": "please review"}),
    ("changes_requested", {"reason": "fix lint"}),
    ("unblocked", None),
    ("crashed", {"pid": 1}),
    ("timed_out", {"limit_seconds": 60}),
]


def test_internal_events_are_silent_everywhere(board, monkeypatch):
    tid = _task()
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _sub(tid, platform="telegram", chat_id=ORIGIN_CHAT, user_id=ORIGIN_CHAT, chat_type="dm",
         notifier_profile="default", delivery_mode="notify+wake", delivery_metadata=_origin_meta(chat_type="dm"))
    for kind, payload in INTERNAL:
        _event(tid, kind, payload)
    _age_events(tid, 3600)  # even stale internal events must not trigger a fallback

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter), n=2)
    assert adapter.sent == [] and adapter.handled == []
    assert _fallback_rows(tid) == []
    # Desktop origin also stays quiet (no synthetic agent turn for churn).
    assert _collect_kanban_notifications({"session_key": DESKTOP_KEY}) == []


def test_actionable_status_blocked_still_reaches_origin(board, monkeypatch):
    tid = _task()
    _sub(tid, platform="telegram", chat_id=ORIGIN_CHAT, user_id=ORIGIN_CHAT, chat_type="dm",
         notifier_profile="default", delivery_mode="notify", delivery_metadata=_origin_meta(chat_type="dm"))
    _event(tid, "status", {"status": "blocked"})
    _event(tid, "gave_up", {"failures": 2, "error": "boom"})

    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter))
    assert len(adapter.sent) == 2


# --- Compatibility ----------------------------------------------------------


def test_legacy_mode_keeps_historical_mirror_delivery(board, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    tid = _task()
    _desktop_origin(tid)
    _blanket_mirror(tid)
    _complete(tid)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    _tick(monkeypatch, runner)
    assert [s["chat_id"] for s in adapter.sent] == [HOME_CHAT]


def test_kanban_create_auto_subscribe_stamps_origin(board, monkeypatch):
    from tools import kanban_tools

    monkeypatch.setattr(kanban_tools, "_resolve_notify_target", lambda: dict(
        platform="tui", chat_id=DESKTOP_KEY, chat_type=None, thread_id=None, user_id=None,
        user_id_alt=None, notifier_profile="default", delivery_mode=None, delivery_metadata=None))
    tid = _task()
    conn = kbc.connect()
    try:
        assert kanban_tools._maybe_auto_subscribe(conn, tid) is True
    finally:
        conn.close()
    (row,) = _subs(tid)
    assert row["delivery_metadata"][ROUTE_ROLE_KEY] == "origin"


def test_child_inherits_parent_origin_role(board, monkeypatch):
    parent = _task(title="parent")
    _desktop_origin(parent)
    conn = kbc.connect()
    try:
        child = kb.create_task(conn, title="child", assignee="gohanreviewer", parents=(parent,))
    finally:
        conn.close()
    (row,) = _subs(child)
    assert row["platform"] == "tui" and row["delivery_metadata"][ROUTE_ROLE_KEY] == "origin"
