# Agent Loop System

## Setup and gates

- Install dev tools with `python3 -m pip install -e ".[dev]"`; there is no lockfile or alternate package manager.
- Canonical gate: `python -m pytest -q`, `ruff check .`, `python -m compileall -q src examples tests`, `python -m build`.
- CI targets Python 3.11-3.14.
- Use focused tests during TDD, then run the full gate.

## Public commands

- `agent-loop`: original bounded worker/evaluator feedback loop.
- `agent-loop-control`: operator and supervisor control-plane CLI.
- `agent-loop-worker`: capability-scoped worker client for the Unix socket API.
- `agent-autonomy`: optional runner-specific autonomy adapter; not part of the generic core.

Without installation, use `PYTHONPATH=src python3 -m agent_loop.<module>`.

## Architecture map

- `persistence.py`: SQLite/WAL connections, owner-only database files, transactions, append-only events.
- `workflow.py`: missions, dependency DAGs, tasks, claims, leases, heartbeats, resource locks, retries, lifecycle, and recovery.
- `message_board.py`: typed messages, durable subscription cursors, deduplication, and versioned owner-scoped facts.
- `artifacts.py`: content-addressed immutable blobs, provenance, permissions, and integrity checks.
- `action_broker.py`: exact capability grants, R0-R4 policy, payload-bound approvals, handler receipts, unknown-outcome recovery, and budgets.
- `runner_adapter.py`: strict JSON subprocess contract, allowlists, workspace roots, per-run cgroups, timeout, cancellation, output cap, privilege drop, and run-token redaction.
- `coordinator.py`: recover → claim → issue scoped token → run → validate evidence/artifacts → complete/fail → revoke token.
- `worker_api.py`: run-token hashes and capability-scoped Unix socket methods.
- `control_cli.py`: operator/supervisor command surface.
- `worker_cli.py`: scoped worker command surface; must never expose approval or action execution.
- `engine.py` / `command_runner.py`: original feedback-loop harness.
- `autonomy.py` / `hermes_control.py`: optional runtime adapter layer. Generic modules must not import these.

## Core invariants

- The database is canonical; transcripts are not state.
- Workers never receive direct database, policy, approval, receipt, audit-write, or shutdown access.
- Claims, resource leases, fact CAS updates, action transitions, and budget reservations serialize through `BEGIN IMMEDIATE`.
- Run IDs are fencing tokens. Stale workers cannot heartbeat or complete.
- Delivery is at least once. Stable idempotency keys are required at redelivery boundaries.
- Shared facts use owner checks and versions. Disagreement uses typed messages.
- Artifact IDs count as evidence only after existence, hash, size, mission, and task checks.
- Approval binds the canonical action payload hash.
- Ambiguous external outcomes become `unknown` and are never retried automatically.
- Production cancellation must terminate the entire per-run cgroup; the process-tree fallback is defense in depth only.
- No model/provider/runtime belongs in durable state services.

## Runtime contracts

### Multi-agent worker

- Reads one `RunRequest` JSON object from stdin.
- Receives mission/task/run identity, structured context, workspace, limits, and scoped control values.
- Returns one strict JSON object with `outcome`, `summary`, `artifact_ids`, `evidence`, `fact_proposals`, `residual_risks`, and `requested_followups`.
- Valid outcomes: `candidate_complete`, `blocked`, `failed`.
- The runner uses `shell=False`; commands are non-empty string arrays.
- Operator allowlists constrain executable and resolved cwd.
- Dynamic run tokens, common encodings, and long fragments must be redacted before worker output is parsed or persisted.

### Scoped worker API

- Every call carries the one-run token.
- Identity and scope derive from the token, never caller fields.
- Authorization rechecks current task/run state.
- Methods: context, heartbeat, message publish/list, subscription create/read/ack, fact get/put, artifact put/read, action propose.
- No approval, execution, policy mutation, audit write, or shutdown methods.

### Original feedback loop

- Workers read state JSON from stdin and return `{"output": ..., "metadata": {...}}` or plain text.
- Evaluators receive state/result JSON and must return a real boolean `passed`.
- Preserve allowlist, cwd/env, container, timeout, output cap, and redaction behavior.

## Tests

- Real boundaries are mandatory: SQLite files, multiprocessing races, subprocesses, process groups, Unix sockets, artifact files, and CLI parsing.
- `test_workflow_control_plane.py`: mission/task state machines, DAGs, leases, recovery, resource conflicts.
- `test_message_board.py`: typed board, cursors, dedupe, facts.
- `test_action_broker.py`: capabilities, approvals, receipts, budgets.
- `test_runner_adapter.py`: process protocol, timeout/cancel, coordinator, scoped token integration.
- `test_worker_api.py` / `test_worker_cli.py`: authorization and real socket calls.
- `test_control_plane_adversarial.py`: cross-scope, secret, symlink, workspace, and permission failures.
- `test_control_cli.py`: real operator and daemon flows.
- `test_public_api.py`: package boundary and entrypoints.

## Local state

- `.agent-loop/`, `.omo/`, `var/`, database/WAL/SHM files, sockets, and `config/autonomy-policy.json` are local-only and ignored.
- Public policy examples use `*.example.json`.
- Never commit credentials, generated registries, task databases, artifacts, or private paths.

## Documentation

- `README.md`: public overview and quick start.
- `docs/ARCHITECTURE.md`: component, protocol, state, trust, and recovery design.
- `docs/OPERATIONS.md`: installation, canary, service, backup, and operator runbook.
- `SECURITY.md`: threat model, enforced controls, deployment requirements, and limits.
