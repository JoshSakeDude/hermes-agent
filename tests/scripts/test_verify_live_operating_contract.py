"""Regression tests for the full live operating-contract verifier."""

from __future__ import annotations

import importlib.util
import asyncio
import os
import socket
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-live-operating-contract.py"
SPEC = importlib.util.spec_from_file_location("verify_live_operating_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_review_binding_requires_literal_full_sha():
    sha = "a" * 40
    assert verifier.literal_commit_sha(sha) == sha
    assert verifier.literal_commit_sha(sha.upper()) == sha
    assert verifier.literal_commit_sha("HEAD") is None
    assert verifier.literal_commit_sha("main") is None
    assert verifier.literal_commit_sha("a" * 39) is None
    assert verifier.literal_commit_sha(None) is None


def _fake_proc(tmp_path: Path, pid: int, repo: Path, cmdline: bytes = b"hermes\0gateway\0run\0") -> Path:
    proc_root = tmp_path / "proc"
    proc = proc_root / str(pid)
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(cmdline)
    (proc / "cwd").symlink_to(repo, target_is_directory=True)
    return proc_root


def test_running_gateway_attestation_reads_live_control_socket(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = "b" * 40
    pid = 4242
    home = tmp_path / ".hermes"
    home.mkdir()
    proc_root = _fake_proc(tmp_path, pid, repo)
    identity = {
        "pid": pid,
        "kind": "hermes-gateway",
        "boot_code_sha": sha,
        "boot_repo": str(repo),
        "hermes_home": str(home),
    }

    result = verifier.check_running_gateway(
        home, sha, repo, proc_root=proc_root,
        query_fn=lambda queried_home: (identity, pid),
    )
    assert result["matches"] is True
    assert result["returncode"] == 0


def test_running_gateway_attestation_rejects_non_gateway_process(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = "c" * 40
    pid = 5252
    home = tmp_path / ".hermes"
    home.mkdir()
    proc_root = _fake_proc(tmp_path, pid, repo, cmdline=b"sleep\0" b"30\0")
    forged = {
        "pid": pid,
        "kind": "not-a-gateway",
        "boot_code_sha": sha,
        "boot_repo": str(repo),
        "hermes_home": str(home),
    }

    result = verifier.check_running_gateway(
        home, sha, repo, proc_root=proc_root,
        query_fn=lambda queried_home: (forged, pid),
    )
    assert result["matches"] is False
    assert result["returncode"] == 1


def test_running_gateway_attestation_rejects_wrong_sha_independently(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / ".hermes"
    home.mkdir()
    pid = 6262
    proc_root = _fake_proc(tmp_path, pid, repo)
    identity = {
        "pid": pid,
        "kind": "hermes-gateway",
        "boot_code_sha": "e" * 40,
        "boot_repo": str(repo),
        "hermes_home": str(home),
    }
    result = verifier.check_running_gateway(
        home, "f" * 40, repo, proc_root=proc_root,
        query_fn=lambda queried_home: (identity, pid),
    )
    assert result["matches"] is False


def test_running_gateway_attestation_rejects_missing_live_socket(tmp_path):
    result = verifier.check_running_gateway(
        tmp_path / ".hermes", "f" * 40, tmp_path,
        query_fn=lambda queried_home: None,
    )
    assert result["matches"] is False


def test_running_gateway_attestation_rejects_peer_pid_mismatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / ".hermes"
    home.mkdir()
    pid = 7272
    proc_root = _fake_proc(tmp_path, pid, repo)
    identity = {
        "pid": pid,
        "kind": "hermes-gateway",
        "boot_code_sha": "a" * 40,
        "boot_repo": str(repo),
        "hermes_home": str(home),
    }
    result = verifier.check_running_gateway(
        home, "a" * 40, repo, proc_root=proc_root,
        query_fn=lambda queried_home: (identity, pid + 1),
    )
    assert result["matches"] is False


def test_account_home_ignores_environment(monkeypatch, tmp_path):
    import pwd

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fake-hermes"))
    expected = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert verifier.account_home() == expected


def test_live_command_env_overrides_caller_home_and_profile(monkeypatch, tmp_path):
    canonical = tmp_path / "real-home"
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fake-hermes"))
    monkeypatch.setenv("HERMES_PROFILE", "fixture")

    env = verifier.live_command_env(canonical)
    assert env["HOME"] == str(canonical)
    assert env["HERMES_HOME"] == str(canonical / ".hermes")
    assert "HERMES_PROFILE" not in env


def test_live_socket_peer_pid_is_kernel_authenticated(tmp_path):
    from gateway.control_socket import GatewayControlServer, identify_gateway_with_peer_pid

    if not hasattr(socket, "SO_PEERCRED"):
        return
    home = tmp_path / ".hermes"
    home.mkdir()

    async def scenario():
        server = GatewayControlServer(
            home, verb_handlers={"identify": lambda: {"pid": 999999}},
        )
        assert await server.start()
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: identify_gateway_with_peer_pid(home))
        finally:
            await server.stop()

    answer = asyncio.run(scenario())
    assert answer is not None
    identity, peer_pid = answer
    assert identity["pid"] == 999999
    assert peer_pid == os.getpid()
