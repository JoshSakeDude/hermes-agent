#!/usr/bin/env python3
"""Fail-closed pre/post-update contract canary for Hermes source installs.

This verifies behavioral code paths without sending external messages. Default
mode requires a clean checkout bound to an explicitly reviewed SHA. During
active development, ``--allow-dirty`` is permitted but the result is labeled
non-promotable and must never be used as deployment evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

EXPECTED_ROUTING = "origin_first"
EXPECTED_STALE_SECONDS = "600"
CONTRACT_TESTS = (
    "tests/hermes_cli/test_kanban_db.py",
    "tests/hermes_cli/test_kanban_worker_attempt_log.py",
    "tests/hermes_cli/test_kanban_block_classify.py",
    "tests/gateway/test_kanban_origin_first_routing.py",
    "tests/gateway/test_wake_delivery.py",

    "tests/tui_gateway/test_kanban_notify_poller.py",
    "tests/pm/test_source_update_launch.py",
    "tests/hermes_cli/test_kanban_worker_lifecycle_hooks.py",
    "tests/scripts/test_verify_live_operating_contract.py",
)


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> dict:
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def git_text(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30, check=True,
    )
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def literal_commit_sha(value: str | None) -> str | None:
    """Accept only a literal 40-hex reviewed SHA, never HEAD/branch/tag."""
    if value and re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    return None


def account_home() -> Path:
    """OS account home, independent of caller-controlled HOME/HERMES_HOME."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def live_command_env(home: Path) -> dict[str, str]:
    """Subprocess environment pinned to canonical live configuration."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(home / ".hermes")
    env.pop("HERMES_PROFILE", None)
    return env


def load_gateway_peer_query(repo: Path):
    """Import the control-socket client from the checkout under review."""
    repo = repo.resolve()
    sys.path.insert(0, str(repo))
    try:
        importlib.invalidate_caches()
        module = importlib.import_module("gateway.control_socket")
    finally:
        sys.path.pop(0)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise RuntimeError("gateway control-socket client has no source file")
    source = Path(module_file).resolve()
    expected_source = repo / "gateway" / "control_socket.py"
    if source != expected_source:
        raise RuntimeError(
            f"gateway control-socket client imported from {source}, expected {expected_source}"
        )
    return module.identify_gateway_with_peer_pid


def check_running_gateway(home: Path, expected_sha: str, repo: Path, *, query_fn=None,
                          proc_root: Path = Path("/proc")) -> dict:
    """Read boot identity from the live gateway-owned local control socket."""
    try:
        if query_fn is None:
            query_fn = load_gateway_peer_query(repo)
        answer = query_fn(home)
        if not (isinstance(answer, tuple) and len(answer) == 2 and isinstance(answer[0], dict)):
            raise RuntimeError("live gateway control socket did not answer identify")
        identity, peer_pid = answer
        pid = int(identity["pid"])
        proc = proc_root / str(pid)
        cwd = (proc / "cwd").resolve()
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        matches = (
            identity.get("kind") == "hermes-gateway"
            and peer_pid == pid
            and identity.get("boot_code_sha") == expected_sha
            and Path(identity.get("boot_repo", "")).resolve() == repo
            and Path(identity.get("hermes_home", "")).resolve() == home
            and cwd in (repo, home)
            and "gateway" in cmdline
        )
        return {
            "returncode": 0 if matches else 1,
            "matches": matches,
            "pid": pid,
            "peer_pid": peer_pid,
            "boot_sha": identity.get("boot_code_sha"),
            "cwd": str(cwd),
            "live_control_socket": True,
        }
    except Exception as exc:
        return {"returncode": 1, "matches": False, "error": str(exc)}


def check_live_routes(db_path: Path, topic_map_path: Path) -> dict:
    try:
        topic_map = json.loads(topic_map_path.read_text(encoding="utf-8"))
        ops_chat = str(topic_map["chat_id"])
        topics = topic_map["topics"]
        required = {role: str(topics[role]) for role in ("tasks", "approvals", "ops", "alerts")}
        if len(set(required.values())) != len(required):
            raise ValueError("topic role thread ids are not distinct")
        leaks = []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT task_id, platform, chat_id, thread_id, delivery_metadata "
                "FROM kanban_notify_subs WHERE platform='telegram'"
            ).fetchall()
        finally:
            conn.close()
        role_thread = {
            "alerts": required["alerts"],
            "approvals": required["approvals"],
            "ops": required["ops"],
            "fallback": required["alerts"],
        }
        for row in rows:
            try:
                metadata = json.loads(row["delivery_metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            role = metadata.get("route_role")
            if role in role_thread and (
                str(row["chat_id"]) != ops_chat or str(row["thread_id"] or "") != role_thread[role]
            ):
                leaks.append({"task_id": row["task_id"], "role": role})
        return {
            "returncode": 0 if not leaks else 1,
            "matches": not leaks,
            "ops_chat_configured": bool(ops_chat),
            "topic_roles": sorted(required),
            "misrouted_role_subscriptions": leaks,
        }
    except Exception as exc:
        return {"returncode": 1, "matches": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=os.environ.get("HERMES_PYTHON", sys.executable))
    parser.add_argument("--expected-sha", help="reviewed commit SHA; required for promotable evidence")
    parser.add_argument("--allow-dirty", action="store_true", help="development only; result is non-promotable")
    parser.add_argument("--live-config", action="store_true")

    args = parser.parse_args()

    repo = args.repo.resolve()
    launcher = repo / ".hermes" / "bin" / "hermes"
    head = git_text(repo, "rev-parse", "HEAD")
    status = git_text(repo, "status", "--porcelain=v1", "--untracked-files=all")
    expected_sha = literal_commit_sha(args.expected_sha)
    revision_matches = expected_sha is not None and head == expected_sha
    clean = not status
    revision_ok = (clean and revision_matches) or args.allow_dirty

    report: dict[str, object] = {
        "repo": str(repo),
        "head": head,
        "expected_sha": args.expected_sha,
        "clean": clean,
        "promotable": clean and revision_matches and not args.allow_dirty,
        "checks": {},
    }
    checks: dict[str, object] = report["checks"]  # type: ignore[assignment]
    checks["revision_binding"] = {
        "returncode": 0 if revision_ok else 1,
        "matches": revision_ok,
        "dirty_paths": status.splitlines(),
        "development_override": args.allow_dirty,
    }
    checks["diff_check"] = run(["git", "diff", "--check"], cwd=repo)
    checks["launcher_identity"] = {
        "returncode": 0 if launcher.is_file() else 1,
        "path": str(launcher),
        "sha256": sha256(launcher) if launcher.is_file() else None,
    }

    with tempfile.TemporaryDirectory(prefix="hermes-contract-") as scratch:
        scratch_path = Path(scratch)
        env = {
            "HOME": str(scratch_path),
            "HERMES_HOME": str(scratch_path / ".hermes"),
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }
        checks["installation_launcher_clean_context"] = run(
            [str(launcher), "--version"], cwd=scratch_path, env=env,
        )

    test_env = dict(os.environ)
    test_env["HERMES_PYTHON"] = args.python
    checks["behavioral_contract"] = run(
        [str(repo / "scripts" / "run_tests.sh"), *CONTRACT_TESTS, "-q"],
        cwd=repo, env=test_env,
    )

    if args.live_config:
        # This is intentionally not HERMES_HOME-driven: a caller may not point
        # a promotion gate at a disposable fixture and call it live evidence.
        hermes_home = account_home() / ".hermes"
        live_db = hermes_home / "kanban.db"
        topic_map = hermes_home / "telegram_topics.json"
        if expected_sha is None:
            checks["live_inputs"] = {
                "returncode": 1,
                "matches": False,
                "error": "--live-config requires a literal 40-hex --expected-sha",
            }
        else:
            live_env = live_command_env(account_home())
            routing = run(
                [str(launcher), "config", "get", "kanban.notification_routing"],
                cwd=repo, env=live_env,
            )
            stale = run(
                [str(launcher), "config", "get", "kanban.origin_stale_seconds"],
                cwd=repo, env=live_env,
            )
            routing["matches"] = routing["returncode"] == 0 and routing["stdout"].strip() == EXPECTED_ROUTING
            stale["matches"] = stale["returncode"] == 0 and stale["stdout"].strip() == EXPECTED_STALE_SECONDS
            checks["live_routing_config"] = routing
            checks["live_stale_seconds"] = stale
            checks["live_destination_matrix"] = check_live_routes(live_db, topic_map)
            checks["running_service_revision"] = check_running_gateway(
                hermes_home, expected_sha, repo,
            )

    failed = [
        name for name, value in checks.items()
        if not isinstance(value, dict) or value.get("returncode") != 0 or value.get("matches") is False
    ]
    report["ok"] = not failed
    report["promotable"] = bool(report["promotable"]) and not failed
    report["failed"] = failed
    print(json.dumps(report, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
