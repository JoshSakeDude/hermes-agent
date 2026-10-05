"""A model picked mid-turn must still get its selection-guard confirm step.

``config.set model`` on a *running* session cannot swap the agent in place --
the worker thread is reading ``agent.model`` / ``agent.client`` on every
iteration -- so it stashes the pick in ``session["pending_model_switch"]`` and
``_apply_pending_model_switch`` applies it at the next turn start.

That deferral used to skip the selection guards entirely: the stash branch
answered ``confirm_required: False`` without ever calling them. A client that
implements the confirm round-trip was therefore told no consent was needed, so
it never prompted. One turn later ``_apply_pending_model_switch`` ran the
guards with the stashed (unconfirmed) flag, saw the warning, and deliberately
dropped the switch -- correct on its own terms, but by then no round-trip was
possible. The user's pick silently reverted and the confirm was never offered
on this path at all.
"""

import threading
import types

import pytest

from tui_gateway import server

# A vendor-documented data-training tier. The data-policy guard keys on the
# model id alone (no base_url / api_key / model_info), which is exactly what
# the stash branch can see before resolution.
GUARDED_MODEL = "muse-spark-1.2-contributor"
UNGUARDED_MODEL = "anthropic/claude-sonnet-4.6"
LARGE_CONTEXT_TARGET = "anthropic/claude-haiku-4.5"

def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }

def _config_set_model(value, **extra_params):
    params = {"session_id": "sid", "key": "model", "value": value}
    params.update(extra_params)

    return server.handle_request({"id": "1", "method": "config.set", "params": params})

@pytest.fixture
def running_session(monkeypatch):
    """A busy session whose live swap path is fatal if it is ever reached."""

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "_apply_model_switch ran on the busy path -- it would race the "
            "worker thread reading agent.model / agent.client"
        )

    monkeypatch.setattr(server, "_apply_model_switch", _must_not_run)
    server._sessions["sid"] = _session(running=True)
    try:
        yield server._sessions["sid"]
    finally:
        server._sessions.pop("sid", None)


@pytest.fixture
def running_large_context_session(monkeypatch):
    """A busy session with enough measured context to trigger the cache-cost guard."""

    agent = types.SimpleNamespace(
        model="anthropic/claude-sonnet-4.6",
        provider="anthropic",
        context_compressor=types.SimpleNamespace(last_prompt_tokens=225_000),
    )
    monkeypatch.setattr(
        "hermes_cli.model_selection_guards._context_cache_threshold", lambda: 100_000
    )
    server._sessions["sid"] = _session(running=True, agent=agent)
    try:
        yield server._sessions["sid"]
    finally:
        server._sessions.pop("sid", None)

class TestGuardedPickAsksBeforeStashing:
    def test_reports_confirm_required_instead_of_deferring(self, running_session):
        resp = _config_set_model(GUARDED_MODEL)

        assert not resp.get("error")
        result = resp["result"]
        assert result["confirm_required"] is True, (
            "the deferred path answered confirm_required=False without running "
            "the guards, so a correct client never prompts and the pick is "
            "dropped a turn later with no way to consent"
        )
        assert result["confirm_message"].strip()
        assert result["deferred"] is False

    def test_leaves_the_session_untouched(self, running_session):
        _config_set_model(GUARDED_MODEL)

        assert "pending_model_switch" not in running_session, (
            "an unconfirmed guarded pick must not be queued -- the next turn "
            "start would drop it anyway, after the pill already moved"
        )

    def test_reconfirming_queues_the_pick(self, running_session):
        resp = _config_set_model(GUARDED_MODEL, confirm_expensive_model=True)

        result = resp["result"]
        assert result["deferred"] is True
        assert result["confirm_required"] is False

        pending = running_session["pending_model_switch"]
        assert pending["raw"] == GUARDED_MODEL
        assert pending["confirm_expensive_model"] is True, (
            "the ack must survive into the stash or _apply_pending_model_switch "
            "re-runs the guard at turn start and drops the confirmed pick"
        )

class TestUnguardedPickStillDefers:
    """The queue-don't-race behaviour is the whole point of this branch."""

    def test_defers_without_a_confirm_step(self, running_session):
        result = _config_set_model(UNGUARDED_MODEL)["result"]

        assert result["deferred"] is True
        assert result["confirm_required"] is False
        assert result["confirm_message"] == ""
        assert result["value"] == UNGUARDED_MODEL

    def test_stashes_the_pick_for_the_next_turn(self, running_session):
        _config_set_model(UNGUARDED_MODEL)

        pending = running_session["pending_model_switch"]
        assert pending["raw"] == UNGUARDED_MODEL
        assert pending["confirm_expensive_model"] is False

    def test_explicit_provider_is_still_recorded_for_display(self, running_session):
        _config_set_model(f"{UNGUARDED_MODEL} --provider anthropic")

        pending = running_session["pending_model_switch"]
        assert pending["display_provider"] == "anthropic"


class TestLargeContextPickAsksBeforeStashing:
    def test_reports_confirm_required_with_live_context(self, running_large_context_session):
        result = _config_set_model(LARGE_CONTEXT_TARGET)["result"]

        assert result["confirm_required"] is True
        assert result["deferred"] is False
        assert "225,000 tokens" in result["confirm_message"]
        assert "pending_model_switch" not in running_large_context_session

    def test_confirmed_pick_commits_at_next_turn_start(
        self, running_large_context_session, monkeypatch
    ):
        result = _config_set_model(
            LARGE_CONTEXT_TARGET, confirm_expensive_model=True
        )["result"]
        assert result["deferred"] is True
        assert running_large_context_session["pending_model_switch"][
            "confirm_expensive_model"
        ] is True

        applied = []

        def _apply(sid, session, raw, **kwargs):
            applied.append((sid, raw, kwargs["confirm_expensive_model"]))
            session["agent"].model = LARGE_CONTEXT_TARGET
            return {"value": LARGE_CONTEXT_TARGET, "confirm_required": False, "warning": ""}

        monkeypatch.setattr(server, "_apply_model_switch", _apply)
        running_large_context_session["running"] = False
        server._apply_pending_model_switch("sid", running_large_context_session)

        assert applied == [("sid", LARGE_CONTEXT_TARGET, True)]
        assert running_large_context_session["agent"].model == LARGE_CONTEXT_TARGET
        assert "pending_model_switch" not in running_large_context_session


class TestTurnStartRefusalIsVisible:
    @pytest.mark.parametrize(
        ("result", "raised", "reason"),
        [
            ({"confirm_required": True, "confirm_message": "confirmation required"}, None,
             "confirmation required"),
            (None, RuntimeError("resolver unavailable"), "resolver unavailable"),
        ],
    )
    def test_repaints_real_model_and_emits_specific_notice(
        self, monkeypatch, result, raised, reason
    ):
        agent = types.SimpleNamespace(model="old-model", provider="old-provider")
        session = _session(agent=agent, pending_model_switch={
            "raw": "new-model --provider new-provider",
            "display_model": "new-model",
            "display_provider": "new-provider",
            "confirm_expensive_model": False,
        })
        emitted = []

        def _apply(*_args, **_kwargs):
            if raised is not None:
                raise raised
            return result

        monkeypatch.setattr(server, "_apply_model_switch", _apply)
        monkeypatch.setattr(
            server, "_session_info",
            lambda live_agent, _session: {"model": live_agent.model, "provider": live_agent.provider},
        )
        monkeypatch.setattr(
            server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload))
        )

        server._apply_pending_model_switch("sid", session)

        assert emitted[0] == (
            "session.info", "sid", {"model": "old-model", "provider": "old-provider"}
        )
        assert emitted[1][0] == "notice"
        assert f"model switch to new-model not applied: {reason}" in emitted[1][2]["message"]
        assert not any(event == "error" for event, _sid, _payload in emitted)

class TestGuardFailureIsNotFatal:
    def test_a_raising_guard_falls_back_to_deferring(self, running_session, monkeypatch):
        """A broken guard must never cost the user their model pick.

        The apply-time check in ``_apply_pending_model_switch`` is still there,
        so failing open here degrades to the old behaviour rather than to a
        silently unguarded switch.
        """

        def _boom(*_args, **_kwargs):
            raise RuntimeError("guard table is broken")

        monkeypatch.setattr(
            "hermes_cli.model_selection_guards.combined_selection_warning", _boom
        )

        result = _config_set_model(GUARDED_MODEL)["result"]

        assert result["deferred"] is True
        assert running_session["pending_model_switch"]["raw"] == GUARDED_MODEL
