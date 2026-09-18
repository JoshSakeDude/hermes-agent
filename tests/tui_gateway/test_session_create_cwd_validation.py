"""Session creation rejects unusable explicit workspace paths."""

from tui_gateway import server


def test_session_create_rejects_missing_explicit_cwd(monkeypatch, tmp_path):
    missing = tmp_path / "mac-only-project"
    fallback = tmp_path / "backend-home"
    fallback.mkdir()
    before = set(server._sessions)

    monkeypatch.setattr(server, "_completion_cwd", lambda params=None: str(fallback))
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)

    response = server._methods["session.create"](
        "create-invalid-cwd",
        {"cwd": str(missing), "source": "desktop"},
    )

    assert response["error"]["code"] == 4000
    assert "working directory does not exist" in response["error"]["message"]
    assert set(server._sessions) == before
