"""Session creation validates explicit workspaces on the backend that owns them."""

import pytest

from tui_gateway import server


@pytest.fixture
def inert_session_create(monkeypatch, tmp_path):
    fallback = tmp_path / "backend-home"
    fallback.mkdir()
    monkeypatch.setattr(server, "_completion_cwd", lambda params=None: str(params.get("cwd") or fallback))
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    before = set(server._sessions)
    yield
    for sid in set(server._sessions) - before:
        server._sessions.pop(sid, None)


def test_session_create_rejects_missing_explicit_local_cwd(monkeypatch, tmp_path, inert_session_create):
    missing = tmp_path / "mac-only-project"
    before = set(server._sessions)
    monkeypatch.setattr(server, "_bound_terminal_backend", lambda _profile_home: "local")

    response = server._methods["session.create"](
        "create-invalid-cwd", {"cwd": str(missing), "cwd_explicit": True, "source": "desktop"})

    assert response["error"]["code"] == 4000
    assert "working directory does not exist" in response["error"]["message"]
    assert set(server._sessions) == before


@pytest.mark.parametrize("backend", ["ssh", "docker"])
def test_session_create_does_not_host_validate_remote_or_container_cwd(
    monkeypatch, tmp_path, inert_session_create, backend
):
    missing = tmp_path / f"{backend}-only-project"
    monkeypatch.setattr(server, "_bound_terminal_backend", lambda _profile_home: backend)

    response = server._methods["session.create"](
        f"create-{backend}-cwd", {"cwd": str(missing), "cwd_explicit": True, "source": "desktop"})

    assert "error" not in response
    session = server._sessions[response["result"]["session_id"]]
    assert session["explicit_cwd"] is True
    assert session["cwd"] == str(missing)