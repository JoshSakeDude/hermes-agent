"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                protection = None if state.get("no_protection") else {
                    "requiredStatusChecks": [{"context": "required", "app": {"databaseId": 1}}]}
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": protection}}}}}
            elif "/rules/branches/" in self.path and state.get("rules_denied"):
                self.send_error(403)
                return
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                runs = [{"id": 42 + i, "name": context, "head_sha": sha,
                         "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                         "html_url": f"https://github.com/acme/repo/actions/runs/{42 + i}"}
                        for i, context in enumerate(state.get("required_names", ["required"]))]
                if state.get("stale"):
                    for run in runs:
                        run["head_sha"] = "b" * 40
                if state.get("missing"):
                    runs = []
                optional = {"name": "optional", "head_sha": sha, "app": {"id": 1},
                            "status": "completed", "conclusion": "skipped"}
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**optional, "id": 1000 + i} for i in range(100)
                ]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("malformed_total"):
                    value = [{"total_count": True, "check_runs": runs[:1]}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[{"id": 99, "context": "legacy", "state": "success",
                           "target_url": "https://ci.example.test/build/9?token=secret#fragment"}]] \
                    if state.get("legacy_status") else [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json,sys,urllib.error,urllib.request\n"
                  "if '--slurp' in sys.argv:\n"
                  "    print('unknown flag: --slurp', file=sys.stderr); sys.exit(1)\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "try:\n"
                  "    value=json.loads(urllib.request.urlopen(u).read().decode())\n"
                  "except urllib.error.HTTPError as exc:\n"
                  "    print(f'gh: HTTP {exc.code}', file=sys.stderr); sys.exit(1)\n"
                  "if '--paginate' in sys.argv:\n"
                  "    print(''.join(json.dumps(page) for page in value))\n"
                  "else:\n"
                  "    print(json.dumps(value))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("posix")
def test_paginated_api_does_not_require_gh_slurp(tmp_path, monkeypatch):
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys\n"
                  "if '--slurp' in sys.argv:\n"
                  "    print('unknown flag: --slurp', file=sys.stderr); sys.exit(1)\n"
                  "print('[{\"id\": 1}][{\"id\": 2}]')\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])

    assert acceptance._api("repos/acme/repo/items", paginate=True) == [
        [{"id": 1}], [{"id": 2}],
    ]


@pytest.mark.platforms("linux")
def test_explicit_checks_are_exact_head_fallback_when_policy_is_unavailable(github):
    contract = json.dumps({
        "pr": "https://github.com/acme/repo/pull/7",
        "checks": ["typecheck", {"context": "production build", "app_id": 1}],
    })
    github.update(no_protection=True, rules_denied=True,
                  required_names=["typecheck", "production build"])
    with connect() as conn:
        tid = kb.create_task(conn, title="explicit", completion_contract=contract)
        assert kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7",
        })
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
        ).fetchone()[0])
    assert receipt["ok"] is True
    assert receipt["head_sha"] == "a" * 40
    assert {check["name"] for check in receipt["checks"]} == {
        "typecheck", "production build",
    }
    assert all(check["classification"] == "success" for check in receipt["checks"])

    for fault, expected in (("missing", "missing"), ("stale", "stale")):
        github[fault] = True
        with connect() as conn:
            tid = kb.create_task(conn, title=f"explicit-{fault}", completion_contract=contract)
            assert not kb.complete_task(conn, tid, result="done", metadata={
                "published_pr": "https://github.com/acme/repo/pull/7",
            })
            failed = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
            ).fetchone()[0])
        assert failed["ok"] is False
        assert expected in {check["classification"] for check in failed["checks"]}
        github.pop(fault)

    for invalid in (
        {"pr": "https://github.com/acme/repo/pull/7", "checks": []},
        {"repo": "acme/repo", "checks": ["duplicate", "duplicate"]},
        {"repo": "acme/repo", "checks": [{"context": "build", "app_id": True}]},
    ):
        with pytest.raises(ValueError):
            acceptance.validate_contract(json.dumps(invalid))


@pytest.mark.platforms("linux")
def test_structured_repo_contract_binds_once_to_the_first_published_pr(github):
    contract = acceptance.validate_contract(json.dumps({"repo": "acme/repo", "checks": ["required"]}))
    github["conclusion"] = "failure"
    with connect() as conn:
        tid = kb.create_task(conn, title="bind structured repo", completion_contract=contract)
        assert not kb.complete_task(conn, tid, result="failed", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7",
        })
        bound = kb.get_task(conn, tid).completion_contract
        assert json.loads(bound) == {
            "pr": "https://github.com/acme/repo/pull/7", "checks": ["required"],
        }
        github["conclusion"] = "success"
        assert not kb.complete_task(conn, tid, result="sibling", metadata={
            "published_pr": "https://github.com/acme/repo/pull/8",
        })
        assert kb.get_task(conn, tid).completion_contract == bound


@pytest.mark.platforms("linux")
def test_malformed_check_run_pagination_fails_closed(github):
    github["malformed_total"] = True
    with connect() as conn:
        tid = kb.create_task(conn, title="malformed pagination", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7",
        })
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
        ).fetchone()[0])
    assert receipt["classification"] == "infra"


def test_duplicate_check_run_pages_fail_closed():
    run = {"id": 42, "name": "required", "head_sha": "a" * 40,
           "app": {"id": 1}, "status": "completed", "conclusion": "success"}
    pages = [
        {"total_count": 1, "check_runs": [run]},
        {"total_count": 1, "check_runs": [dict(run)]},
    ]
    with pytest.raises(ValueError, match="Incomplete check-run pagination"):
        acceptance._check_runs_from_pages(pages)


@pytest.mark.platforms("linux")
def test_legacy_status_receipt_strips_url_query_and_fragment(github):
    contract = acceptance.validate_contract(json.dumps({
        "pr": "https://github.com/acme/repo/pull/7", "checks": ["legacy"],
    }))
    github.update(no_protection=True, rules_denied=True, required_names=[], legacy_status=True)
    with connect() as conn:
        tid = kb.create_task(conn, title="sanitize receipt", completion_contract=contract)
        assert kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7",
        })
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
        ).fetchone()[0])
    assert receipt["checks"][0]["url"] == "https://ci.example.test/build/9"
    assert "secret" not in json.dumps(receipt)


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- Reviewer R1 follow-up tests: policy-unavailable classification + non-success explicit checks ---

@pytest.mark.platforms("linux")
def test_rules_403_without_explicit_checks_is_policy_unavailable_not_auth(github):
    """When the rules endpoint returns 403 and NO explicit checks are declared, the
    classification is policy_unavailable (NOT auth) — it's a plan/permission gap, not
    a credential problem."""
    github.update(no_protection=True, rules_denied=True, required_names=["required"])
    with connect() as conn:
        tid = kb.create_task(conn, title="no checks declared", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7",
        })
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
        ).fetchone()[0])
    assert receipt["classification"] == "policy_unavailable"
    assert "403" in receipt["detail"]
    assert "JSON" in receipt["detail"]


@pytest.mark.platforms("linux")
def test_explicit_check_with_non_success_conclusion_under_403_fallback_fails(github):
    """When rules are 403-denied and explicit checks are declared, a declared check
    whose conclusion is NOT 'success' (e.g. neutral, action_required) must fail the
    acceptance gate."""
    contract = json.dumps({
        "pr": "https://github.com/acme/repo/pull/7",
        "checks": ["required"],
    })
    github.update(no_protection=True, rules_denied=True,
                  required_names=["required"], conclusion="neutral")
    with connect() as conn:
        tid = kb.create_task(conn, title="non-success check", completion_contract=contract)
        assert not kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7",
        })
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
        ).fetchone()[0])
    assert receipt["ok"] is False
    assert receipt["checks"][0]["classification"] != "success"
    assert receipt["checks"][0]["conclusion"] == "neutral"


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]
