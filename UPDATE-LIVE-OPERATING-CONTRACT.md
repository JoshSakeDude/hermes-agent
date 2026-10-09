# Hermes Update: Full Live Operating Contract

An update is not healthy because configuration files exist or a service starts. Promotion requires behavioral evidence from the exact reviewed revision and post-restart readback from the running service.

## Development gate

During implementation only:

```bash
HERMES_PYTHON=/path/to/python scripts/verify-live-operating-contract.py --allow-dirty
```

A dirty result is explicitly `promotable: false`. It may guide development but cannot authorize deployment.

## Promotion gate

Commit the reviewed candidate, then run:

```bash
HERMES_PYTHON=/path/to/python \
  scripts/verify-live-operating-contract.py --expected-sha <reviewed-full-sha>
```

`<reviewed-full-sha>` must be the literal 40-hex SHA from the review handoff. Symbolic or caller-convenience values such as `HEAD`, `main`, or a tag are rejected. The gate records and verifies the HEAD SHA, clean status, launcher path/hash, clean-context launcher execution, source-update launcher publication, and behavioral suites. It fails when the checkout is dirty, the expected SHA differs, or an untracked file exists.

The behavioral contract includes:

1. The installation-owned launcher starts from an unrelated working directory with clean `HOME`, stripped `PATH`, and isolated `HERMES_HOME`.
2. Ready/review dispatcher commands resolve through the installation bootstrap rather than bare store Python.
3. Crash, timeout, and stale-claim breaker events retain the exact originating `run_id`.
4. Crash accounting cannot mutate or block a successor run across the reclaim/accounting transaction gap.
5. Old bound-run and legacy null-run failures are suppressed after an active or completed successor, including equal-second timestamps.
6. Both gateway/Telegram and Desktop/TUI pollers apply run-currentness filtering; gateway delivery performs a final read immediately before external send.
7. The origin-first destination matrix includes negative assertions:
   - Desktop ordinary output does not route to Telegram/Fred.
   - Desktop failures route once to Gohan Ops Alerts, not Fred.
   - Desktop `needs_input` routes once to Gohan Ops Approvals, not Fred.
   - Telegram-origin cards return output to their exact origin without duplicate topic notices.
   - Headless events use configured role topics.
   - The approved Desktop unread fallback remains enabled and targets Gohan Ops Alerts.

The test harness uses disposable boards and recording adapters. It does not send Telegram messages.

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
