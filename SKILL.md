---
name: agent-loop-system
description: Use when building Agent Loop System. Preserve durable control-plane boundaries and verified gates.
---

# Agent Loop System

## Setup

```sh
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
ruff check .
python3 -m compileall -q src examples tests
python3 -m build
```

## Public surfaces

- `agent-loop`: original worker/evaluator feedback loop.
- `agent-loop-control`: operator/supervisor control plane.
- `agent-loop-worker`: scoped agent access over Unix socket.
- `agent-autonomy`: optional runtime adapter, never a generic-core dependency.

## Preserve these boundaries

- Keep SQLite and append-only events canonical; never treat transcripts as state.
- Keep durable services independent from Hermes, model vendors, and agent CLIs.
- Give workers one-run tokens, not database access.
- Derive identity from the token; never accept worker-supplied actor or mission IDs.
- Keep approval, execution, policy, audit-write, and shutdown methods off the worker API.
- Use `BEGIN IMMEDIATE` for claims, leases, CAS facts, action transitions, and budget reservations.
- Use run IDs as fencing tokens; reject stale heartbeat/completion calls.
- Require idempotency keys at redelivery and external-effect boundaries.
- Validate artifact existence, hash, size, mission, and task before accepting completion evidence.
- Bind approvals to the canonical payload hash.
- Mark ambiguous external effects `unknown`; never retry automatically.
- Require delegated per-run cgroups in production; use the Linux subreaper/process-tree path only as fallback defense.
- Never pair `Delegate=yes` with `ProtectControlGroups=true`; that makes the delegated subtree read-only inside a system service.
- Hand task workspaces to `agent-loop-worker:agent-loop-control` with mode `0710`: worker full access, supervisor traversal only for pre-drop `cwd`.
- Keep command arrays and `shell=False`.
- Enforce executable and resolved-workspace allowlists before launch.
- Scrub exact, commonly encoded, and long-fragment token forms from output and every WorkerAPI durable-write route.
- Construct deterministic child environments; never inherit supervisor values implicitly.

## Component map

- `persistence.py`: SQLite/WAL, events, permissions.
- `workflow.py`: missions, DAGs, tasks, leases, lifecycle, recovery.
- `message_board.py`: messages, subscriptions, facts.
- `artifacts.py`: content-addressed outputs.
- `action_broker.py`: capabilities, approvals, receipts, budgets.
- `runner_adapter.py`: strict bounded subprocess protocol.
- `sandbox_exec.py`: trusted cgroup attachment and subreaper descendant cleanup.
- `coordinator.py`: durable dispatch and completion gate.
- `worker_api.py`: run tokens and Unix socket methods.
- `control_cli.py`: operator commands.
- `worker_cli.py`: scoped worker commands.

## Test map

- Workflow and races: `tests/test_workflow_control_plane.py`
- Board and CAS state: `tests/test_message_board.py`
- Actions and budgets: `tests/test_action_broker.py`
- Runner/coordinator/cgroups/subreaper: `tests/test_runner_adapter.py`
- Worker socket/API/CLI: `tests/test_worker_api.py`, `tests/test_worker_cli.py`
- Operator CLI and daemon canary: `tests/test_control_cli.py`
- Adversarial boundaries: `tests/test_control_plane_adversarial.py`
- Package surface: `tests/test_public_api.py`

New behavior requires a failing boundary test first. After targeted green, run the complete gate, a clean-install CLI smoke test, and a real enabled-service canary before calling deployment complete.

## Local-only state

Never commit:

- `.agent-loop/`
- `.omo/`
- `var/`
- SQLite/WAL/SHM files
- Unix sockets
- `config/autonomy-policy.json`
- credentials or private artifacts

Use `config/*.example.json` for public policy examples.
