# Hermes Update: Full Live Operating Contract

An update is not healthy because configuration files exist or a service starts. Promotion requires behavioral evidence from the exact reviewed revision and post-restart readback from the running service.

## Regression origin

The introducing launcher change was `da18c20226e6de243e9f279686d272224413619b` (`fix: kanban dispatcher prefers module argv over PATH hermes`). It changed dispatcher discovery to a bare interpreter/module command. That became incompatible with the installation-owned dependency-generation contract introduced by the PM store foundation `3d12e86ef13ec5fdbb4e1778ac3f3bf5ad853a74` and consolidated by `5e4a2a3d2407cdc207f05eee543c63a6136540a2`: a store Python can import the source but cannot select the committed dependency generation when started from an unrelated task worktree. The failure shipped in live snapshot `6bae5ad605be53223e8ef9d843488da2228c2c0b`. The repair resolves the owning installation's `installation_command(...)`, preserving `.hermes/bin/hermes` when managed, while an explicit `HERMES_BIN` remains authoritative.

## Development gate

During implementation only:

```bash
scripts/verify-live-operating-contract.py --allow-dirty
```

A dirty result is explicitly `promotable: false`. It may guide development but cannot authorize deployment. By default, leave `HERMES_PYTHON` unset so `scripts/run_tests.sh` selects the installation-managed test environment. Set it only to the interpreter of a complete dev/test environment built with `python -m pm.build_env --source . --out <path> --group dev --group test`; an arbitrary checkout venv may lack contract dependencies and correctly fails the gate.

## Promotion gate

Commit the reviewed candidate, then run:

```bash
scripts/verify-live-operating-contract.py --expected-sha <reviewed-full-sha>
```

`<reviewed-full-sha>` must be the literal 40-hex SHA from the review handoff. Symbolic or caller-convenience values such as `HEAD`, `main`, or a tag are rejected. The gate records and verifies the HEAD SHA, clean status, launcher path/hash, clean-context launcher execution, source-update launcher publication, and behavioral suites. It fails when the checkout is dirty, the expected SHA differs, or an untracked file exists.

The behavioral contract includes:

1. The installation-owned launcher starts from an unrelated working directory with clean `HOME`, stripped `PATH`, and isolated `HERMES_HOME`.
2. Ready/review dispatcher commands resolve through the installation bootstrap rather than bare store Python. The spawn matrix covers `default`, `gohanlite`, and `gohanreviewer`, shell and systemd-like environments, a separate task worktree, automatic discovery, and the explicit `HERMES_BIN` override.
3. Repeated identical crash accounting replaces `last_failure_error`; it never concatenates the same diagnostic across retries.
4. Crash, timeout, and stale-claim breaker events retain the exact originating `run_id`.
5. Crash accounting cannot mutate or block a successor run across the reclaim/accounting transaction gap.
6. Old bound-run and legacy null-run failures are suppressed after an active or completed successor, including equal-second timestamps. Review handoffs remain deliverable after the next lane claims the card.
7. Both gateway/Telegram and Desktop/TUI pollers apply failure-attempt currentness filtering; gateway delivery performs a final read immediately before external send.
8. The origin-first destination matrix includes negative assertions:
   - Desktop ordinary output does not route to Telegram/Fred.
   - Desktop failures route once to Gohan Ops Alerts, not Fred.
   - Desktop `needs_input` routes once to Gohan Ops Approvals, not Fred.
   - Telegram-origin cards return output to their exact origin without duplicate topic notices.
   - Headless events use configured role topics.
   - The approved Desktop unread fallback remains enabled and targets Gohan Ops Alerts.

The test harness uses disposable boards and recording adapters. It does not send Telegram messages. A real `uv` executable must resolve on `PATH` because `tests/pm/test_source_update_launch.py` exercises the source-update launcher contract; missing `uv` is a failed prerequisite, not a skipped test.

## Repeatable update canary

Run the read-only rows before requesting deployment approval and again after restart. Rows marked **approval-gated** create disposable live cards or external delivery and must not run without Josh's explicit approval.

| Contract | Concrete pass condition |
|---|---|
| Candidate identity | Literal reviewed 40-hex SHA, clean checkout, launcher hash recorded, `uv --version` succeeds, and the promotion gate reports `promotable: true`. |
| Carried patches | Every non-comment ref in `~/.hermes/carried-commits.conf` exists at its recorded `custom/*` or `carry/*` SHA; `reapply-carried-commits.sh --check` is `OK`; each configured focused test passes on the candidate. A missing ref, `git cherry` `+` without an intentional port, or absent focused test fails the gate. |
| Profiles | `hermes -p <profile> --version` succeeds for `default`, `gohanlite`, and `gohanreviewer`; the dispatcher matrix proves each uses the installation bootstrap from a separate worktree under both shell and stripped service environments. |
| Cron | Enumerate configured Hermes jobs and system/user cron before and after. Required jobs remain present with identical IDs/schedules/destinations; no unexpected job appears or disappears. Do not execute customer-facing jobs as a canary. |
| Services | Gateway and Desktop backend are healthy; after restart their PIDs/boot times change and gateway `boot_code_sha` equals the reviewed SHA. Dario PID/config remain unchanged. |
| Telegram routing | Disposable-board recording adapters pass the complete positive/negative destination matrix. A real uniquely labeled Telegram probe is **approval-gated** and must assert both the expected topic and forbidden Fred/other-topic destinations. |
| Builder real work | **Approval-gated:** create one disposable `default` builder card in a scratch workspace whose only work is writing a nonce file, reading it back, and completing. Pass only if a worker PID starts, the exact run completes once, and the artifact contains the nonce. |
| Same-card review | **Approval-gated:** on that same disposable card, request `gohanreviewer` review, claim a distinct reviewer run, then approve or request one bounded change. Pass only if `review_requested` remains deliverable after reviewer claim and the lifecycle stays on the same card with exact run IDs. |
| Dependency promotion | **Approval-gated:** create a disposable child gated by the builder card. Before parent completion it must remain `todo`; after verified parent completion it must emit exactly one promotion and become `ready`, then be administratively closed without external work. |
| Stale failure fencing | On a copied/disposable board, insert an old run failure followed by a newer active/completed run. Pass only when gateway and Desktop produce no old-run ping/wake while review handoffs from the immediate predecessor remain eligible. |
| Rollback readiness | Prior SHA exists locally and remotely, rollback dependency generation and launcher are prebuilt in an isolated worktree, SQLite online backup verifies, and the exact compensation commands are recorded. Do not execute rollback during a canary. |

For approval-gated card canaries, use unique titles containing the reviewed SHA and UTC nonce, set `max_iterations` explicitly, use no customer credentials or business data, and archive only after capturing events/runs. Any duplicate event, wrong destination, unexpected retry, or missing readback is a hard failure.

## Post-restart live gate

After an explicitly approved deployment and restart, run:

```bash
scripts/verify-live-operating-contract.py \
  --expected-sha <reviewed-full-sha> \
  --live-config
```

This additionally verifies:

- fixed policy values `notification_routing=origin_first` and `origin_stale_seconds=600`—they are not caller-overridable;
- distinct Tasks, Approvals, Ops, and Alerts topic IDs;
- every role-stamped Telegram subscription points to the configured Gohan Ops chat and exact role thread;
- the gateway process answering the canonical local control socket booted the exact reviewed SHA from the promoted checkout; Linux `SO_PEERCRED` must bind the socket peer PID to the identify response, and its PID cwd, command line, gateway kind, repository, and canonical home must agree;
- the checkout remains clean and still resolves to the reviewed SHA.

Live DB, topic-map, and control-socket paths are fixed to the account home returned by the OS user database; caller-provided `HOME` and `HERMES_HOME` are intentionally ignored so fixtures cannot substitute for live evidence.

A real Telegram delivery probe is stronger evidence of Telegram transport itself, but it is an external action and requires separate explicit approval. If approved, use uniquely labeled disposable canary cards and assert both expected deliveries and forbidden destinations.

## Pre-deployment evidence and rollback staging

Before changing live state, record:

- reviewed candidate SHA and prior live SHA;
- passing promotion-gate JSON;
- live service PID, command, checkout, and active SHA;
- hashes—not contents—of config, launcher, and runtime-facts files;
- a consistent SQLite backup including WAL/SHM handling;
- confirmation that the prior SHA is locally available;
- a prebuilt dependency generation and published launcher for the prior SHA in an isolated rollback worktree.

Staging happens before service interruption. Do not promote a moving branch name.

## Approval-gated deployment transaction

Deployment and rollback cannot be literally atomic across Git, dependency selection, launcher publication, and systemd. Treat them as a bounded transaction with a prepared compensation path; do not claim atomicity.

With explicit approval:

1. Pause the affected service once.
2. Move the live checkout to the exact reviewed SHA.
3. Select the already-built candidate dependency generation and publish its launcher.
4. Start the service once.
5. Run the full post-restart live gate.

If any step or gate fails, stop promotion and execute the prepared compensation path under the same approval:

1. Stop the affected service.
2. Restore the exact prior SHA.
3. Select the prebuilt prior dependency generation and republish the prior launcher.
4. Start the service once.
5. Run the post-restart live gate against the prior SHA.
6. Verify the process checkout/SHA and confirm no candidate worker remains.

Restore the board database only when the update performed an incompatible schema/data mutation. A code rollback alone is not permission to overwrite a live board.

## Approval boundaries

Local tests, worktree preparation, hashes, and read-only inspection need no approval. Live checkout movement, service stop/start/restart, configuration changes, real Telegram messages, database restoration, deployment, and rollback execution require Josh’s explicit approval.
