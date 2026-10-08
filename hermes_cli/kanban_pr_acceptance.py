"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
_CONTRACT_HELP = ("completion_contract must be local-only, OWNER/REPO, an exact GitHub PR URL, "
                  "or JSON with pr/repo plus non-empty checks")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str):
        raise ValueError(_CONTRACT_HELP)
    if _REPO.fullmatch(value) or _PR.fullmatch(value):
        return value
    try:
        contract = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(_CONTRACT_HELP) from None
    target, checks = _validate_structured_contract(contract)
    key = "pr" if _PR.fullmatch(target) else "repo"
    return json.dumps({key: target, "checks": [
        context if app_id is None else {"context": context, "app_id": app_id}
        for context, app_id in checks
    ]}, ensure_ascii=False, separators=(",", ":"))


def _validate_structured_contract(contract: object) -> tuple[str, list[tuple[str, int | None]]]:
    if not isinstance(contract, dict) or set(contract) not in ({"pr", "checks"}, {"repo", "checks"}):
        raise ValueError(_CONTRACT_HELP)
    target = contract.get("pr", contract.get("repo"))
    matcher = _PR if "pr" in contract else _REPO
    if not isinstance(target, str) or not matcher.fullmatch(target):
        raise ValueError(_CONTRACT_HELP)
    raw_checks = contract["checks"]
    if not isinstance(raw_checks, list) or not raw_checks or len(raw_checks) > 100:
        raise ValueError(_CONTRACT_HELP)
    checks = []
    for raw in raw_checks:
        if isinstance(raw, str):
            context, app_id = raw, None
        elif isinstance(raw, dict) and set(raw) in ({"context"}, {"context", "app_id"}):
            context, app_id = raw.get("context"), raw.get("app_id")
            if "app_id" in raw and (isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0):
                raise ValueError(_CONTRACT_HELP)
        else:
            raise ValueError(_CONTRACT_HELP)
        if not isinstance(context, str) or not context.strip() or context != context.strip() or len(context) > 255:
            raise ValueError(_CONTRACT_HELP)
        checks.append((context, app_id))
    if len(set(checks)) != len(checks):
        raise ValueError("completion_contract checks must be unique")
    return target, checks


def _contract_spec(contract: str) -> tuple[str, set[tuple[str, int | None]]]:
    if contract.startswith("{"):
        target, checks = _validate_structured_contract(json.loads(contract))
        return target, set(checks)
    return contract, set()


def bind_contract_to_pr(contract: str, published_pr: str) -> str | None:
    """Bind an OWNER/REPO contract to its first exact PR, preserving explicit checks."""
    match = _PR.fullmatch(published_pr)
    if not match:
        return None
    target, _ = _contract_spec(contract)
    if _PR.fullmatch(target) or target != match[1]:
        return None
    if contract.startswith("{"):
        value = json.loads(contract)
        return validate_contract(json.dumps({"pr": published_pr, "checks": value["checks"]},
                                            ensure_ascii=False))
    return published_pr


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command.append("--paginate")
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}",
                                 status=int(denied[1])) from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = _decode_paginated(result.stdout) if paginate else json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def _decode_paginated(raw: str):
    """Decode the concatenated JSON documents emitted by ``gh api --paginate``.

    ``--slurp`` would wrap those documents, but it is unavailable before gh 2.48.
    Parsing the stream directly keeps the acceptance gate compatible with older gh
    releases without changing its complete-page requirement.
    """
    decoder = json.JSONDecoder()
    pages = []
    cursor = 0
    while cursor < len(raw):
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
        if cursor == len(raw):
            break
        page, cursor = decoder.raw_decode(raw, cursor)
        pages.append(page)
    if not pages:
        raise ValueError("GitHub returned no paginated evidence")
    return pages


def _check_runs_from_pages(pages: object) -> list[dict]:
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing check-run pages")
    totals = []
    runs = []
    for page in pages:
        if not isinstance(page, dict) or set(page) < {"total_count", "check_runs"}:
            raise ValueError("Malformed check-run page")
        total = page["total_count"]
        page_runs = page["check_runs"]
        if isinstance(total, bool) or not isinstance(total, int) or total < 0 or not isinstance(page_runs, list):
            raise ValueError("Malformed check-run pagination metadata")
        totals.append(total)
        for run in page_runs:
            app = run.get("app") if isinstance(run, dict) else None
            if (not isinstance(run, dict) or isinstance(run.get("id"), bool) or
                    not isinstance(run.get("id"), int) or not isinstance(run.get("name"), str) or
                    not isinstance(run.get("head_sha"), str) or not isinstance(app, dict) or
                    isinstance(app.get("id"), bool) or not isinstance(app.get("id"), int) or
                    not isinstance(run.get("status"), str) or
                    (run.get("conclusion") is not None and not isinstance(run.get("conclusion"), str))):
                raise ValueError("Malformed check-run evidence")
            runs.append(run)
    if (len(set(totals)) != 1 or len(runs) != totals[0] or
            len({run["id"] for run in runs}) != totals[0]):
        raise ValueError("Incomplete check-run pagination")
    return runs


def _sanitize_receipt_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    authority = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        profile_home = _assignee_profile_home(assignee)
        target, explicit = _contract_spec(contract)
        declared = _PR.fullmatch(target)
        url = target if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != target) or (declared and published_pr and published_pr != target):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){headRefOid baseRefName state
            baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
                json.dumps(owner), json.dumps(name), number)
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = set(explicit)
        required.update((r["context"], (r.get("app") or {}).get("databaseId"))
                        for r in protection.get("requiredStatusChecks", []))
        try:
            rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                         paginate=True, profile_home=profile_home)
        except _GateAuthError as exc:
            if exc.status == 403:
                if not explicit:
                    receipt.update(classification="policy_unavailable",
                                   detail="Repository rules endpoint returned HTTP 403 (plan/permissions may not include branch-protection rules). "
                                          "Declare explicit required checks via a JSON completion_contract: "
                                          '{"pr": "https://github.com/OWNER/REPO/pull/N", "checks": ["check name", ...]} '
                                          "— then retry completion.")
                    return receipt
                receipt["policy"] = "Repository rules unavailable (HTTP 403); enforcing explicitly declared checks."
            else:
                raise
        else:
            for page in rules:
                for rule in page:
                    if rule["type"] == "required_status_checks":
                        required.update((r["context"], r.get("integration_id"))
                                        for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = _check_runs_from_pages(pages)
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": _sanitize_receipt_url(check.get("html_url") or check.get("target_url")),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
        return receipt
    except subprocess.CalledProcessError:
        # gh CLI command failed (unknown flag, unsupported feature, crash) — never
        # persist stderr (credentials/host details).
        receipt.update(classification="infra",
                       detail="gh CLI command failed; the installed gh version may not support the required API feature.")
        return receipt
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, IndexError):
        # Malformed, incomplete, or missing evidence from a successfully-invoked API.
        receipt.update(classification="infra",
                       detail="GitHub acceptance evidence malformed or incomplete; retry and if persistent, report as a bug.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
