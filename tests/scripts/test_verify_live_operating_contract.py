"""Regression tests for the full live operating-contract verifier."""

from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import socket
import subprocess
import sys
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


def _fake_proc(tmp_path: Path, pid: int, cwd: Path, cmdline: bytes = b"hermes\0gateway\0run\0") -> Path:
    proc_root = tmp_path / "proc"
    proc = proc_root / str(pid)
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(cmdline)
    (proc / "cwd").symlink_to(cwd, target_is_directory=True)
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


def test_running_gateway_attestation_accepts_canonical_service_home_cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = "d" * 40
    pid = 4343
    home = tmp_path / ".hermes"
    home.mkdir()
    proc_root = _fake_proc(tmp_path, pid, home)
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


def test_gateway_query_import_prefers_reviewed_repo_over_stale_editable(tmp_path):
    stale = tmp_path / "stale-editable"
    package = stale / "gateway"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "control_socket.py").write_text(
        "def identify_gateway_with_peer_pid(home):\n    return 'stale'\n",
        encoding="utf-8",
    )
    code = f"""
import importlib.util
import inspect
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("verifier", {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
query = module.load_gateway_peer_query(Path(sys.argv[1]))
print(Path(inspect.getsourcefile(query)).resolve())
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(stale)
    completed = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPT.parents[1])],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()) == SCRIPT.parents[1] / "gateway" / "control_socket.py"


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


def test_running_gateway_attestation_rejects_wrong_repo_independently(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    wrong_repo = tmp_path / "wrong-repo"
    wrong_repo.mkdir()
    home = tmp_path / ".hermes"
    home.mkdir()
    pid = 6363
    proc_root = _fake_proc(tmp_path, pid, home)
    identity = {
        "pid": pid,
        "kind": "hermes-gateway",
        "boot_code_sha": "f" * 40,
        "boot_repo": str(wrong_repo),
        "hermes_home": str(home),
    }
    result = verifier.check_running_gateway(
        home, "f" * 40, repo, proc_root=proc_root,
        query_fn=lambda queried_home: (identity, pid),
    )
    assert result["matches"] is False


def test_running_gateway_attestation_rejects_wrong_home_independently(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / ".hermes"
    home.mkdir()
    wrong_home = tmp_path / "wrong-home"
    wrong_home.mkdir()
    pid = 6464
    proc_root = _fake_proc(tmp_path, pid, home)
    identity = {
        "pid": pid,
        "kind": "hermes-gateway",
        "boot_code_sha": "f" * 40,
        "boot_repo": str(repo),
        "hermes_home": str(wrong_home),
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


def test_behavioral_failure_never_reports_promotable(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    launcher = repo / ".hermes" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher\n", encoding="utf-8")
    sha = "a" * 40

    monkeypatch.setattr(
        verifier,
        "git_text",
        lambda _repo, *args: sha if args[:2] == ("rev-parse", "HEAD") else "",
    )

    def fake_run(command, *, cwd, env=None):
        failed = command and str(command[0]).endswith("run_tests.sh")
        return {"command": command, "returncode": 1 if failed else 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(verifier, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--repo", str(repo), "--expected-sha", sha],
    )

    assert verifier.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == ["behavioral_contract"]
    assert report["ok"] is False
    assert report["promotable"] is False


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
