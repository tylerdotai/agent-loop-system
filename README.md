<a id="readme-top"></a>

[![MIT License][license-shield]][license-url]
[![Python][python-shield]][python-url]

<div align="center">
  <h1>Agent Loop System</h1>
  <p>Durable local multi-agent coordination with bounded runners, explicit policy, and operator control.</p>
</div>

## What it is

Agent Loop System provides two related execution layers:

1. **Closed-loop harness** — `goal → worker → evaluator → feedback → retry → stop`.
2. **Multi-agent control plane** — durable missions, task DAGs, typed messages, leases, artifacts, action approvals, budgets, and crash recovery.

The control plane is intentionally runner-agnostic. Scripts, coding-agent CLIs, local models, hosted models, containers, and remote executors can implement the same JSON worker contract. No model provider or agent framework owns canonical state.

```text
operator
   │
   ▼
mission → task DAG → atomic claim → bounded worker → verified completion
                 │              │
                 │              ├── scoped message board
                 │              ├── versioned facts
                 │              ├── immutable artifacts
                 │              └── governed action proposals
                 ▼
          leases + audit events + recovery
```

## Why it exists

Multi-agent systems fail when transcripts become databases, shared folders become command channels, or models receive broad host authority because prompts say “be careful.”

This project puts coordination and authority in deterministic infrastructure:

- SQLite/WAL is the system of record.
- Workers are disposable processes.
- Messages are typed, attributed, scoped, and deduplicated.
- Task claims and resource leases are atomic.
- Run IDs fence stale workers.
- Shared facts use compare-and-swap versions.
- Artifacts are content-addressed and integrity-checked.
- Actions require exact capabilities and policy-owned risk classes.
- Approval binds the exact payload hash.
- Ambiguous external outcomes become `unknown`, not automatic retries.
- Mission cancellation terminates the complete worker process group.

## Current status

Version `0.2.0` implements a production-shaped **single-host** control plane using the Python standard library plus SQLite.

Implemented:

- durable missions and dependency-aware tasks
- priority, scheduling, retry ceilings, and runtime ceilings
- process-safe task claims
- leases, heartbeats, fencing, and expired-run recovery
- exclusive resource keys
- append-only audit events
- typed message board and durable subscription cursors
- versioned owner-scoped facts
- content-addressed artifacts and provenance
- capability grants, R0-R4 action policy, approvals, denials, and receipts
- atomic budget reservations
- strict JSON subprocess runner
- optional supervisor-owned verification commands
- executable and workspace allowlists
- delegated per-run cgroup containment with a Linux process-tree fallback
- timeout and cancellation that kill detached descendants
- secret redaction, common token-encoding scrubbing, and plaintext-secret rejection
- one-run worker tokens stored only as hashes
- capability-scoped Unix socket worker API
- loopback-only local-model broker with peer-UID and live run-token authorization
- read-only multi-agent repository audit with deterministic evidence IDs
- operator and worker CLIs
- privilege-separated, no-network system-service template

Deliberately not bundled:

- model weights or a model runtime
- a browser dashboard
- generic external side-effect handlers
- multi-host consensus
- a claim of exactly-once external execution

See [Architecture](docs/ARCHITECTURE.md), [Operations](docs/OPERATIONS.md), and [Security](SECURITY.md).

## Requirements

- Python 3.11+
- Linux or another platform supporting Unix-domain sockets for the worker API
- `setpriv` from util-linux for privilege-separated production workers
- delegated cgroup v2 for production workers (`--require-cgroup`)
- Optional: Docker or Podman for custom sandbox adapters

CI targets Python 3.11, 3.12, 3.13, and 3.14.

## Install

```sh
git clone https://github.com/tylerdotai/agent-loop-system.git
cd agent-loop-system
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Public commands:

```sh
.venv/bin/agent-loop --help
.venv/bin/agent-loop-control --help
.venv/bin/agent-loop-worker --help
.venv/bin/agent-loop-model-broker --help
```

`agent-autonomy` is also installed as a compatibility entrypoint for the optional legacy runtime adapter; it is not part of the runner-agnostic control-plane core.

## Quick start: durable control plane

Initialize local state:

```sh
CONTROL="$PWD/.venv/bin/agent-loop-control"
DB="$PWD/.agent-loop/control.db"
ARTIFACTS="$PWD/.agent-loop/artifacts"
WORKSPACES="$PWD/.agent-loop/workspaces"
PYTHON="$PWD/.venv/bin/python"
CANARY="$PWD/examples/control_plane_canary_worker.py"

mkdir -p "$WORKSPACES"
chmod 700 "$WORKSPACES"

"$CONTROL" --db "$DB" --artifacts "$ARTIFACTS" init
```

Create a mission:

```sh
MISSION_ID=$(
  "$CONTROL" --db "$DB" mission-create \
    "Run a governed canary" \
    --actor operator \
    --active \
    --idempotency-key readme-canary:v1 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["mission_id"])'
)
```

Create a task:

```sh
SPEC_JSON=$(
  PYTHON="$PYTHON" CANARY="$CANARY" python3 -c '
import json, os
print(json.dumps({
  "command": [os.environ["PYTHON"], os.environ["CANARY"]],
  "timeout_seconds": 30
}))
'
)

"$CONTROL" --db "$DB" task-create "$MISSION_ID" \
  "Exercise heartbeat, messages, and artifacts" \
  --assignee worker \
  --actor operator \
  --spec-json "$SPEC_JSON" \
  --acceptance-json '{"required_evidence_kinds":["command"],"minimum_artifacts":1}' \
  --max-attempts 1
```

Run one worker cycle:

```sh
"$CONTROL" --db "$DB" --artifacts "$ARTIFACTS" worker-daemon \
  --worker-id canary-1 \
  --role worker \
  --allow-command "$PYTHON" \
  --workspace-root "$WORKSPACES" \
  --capability context.read \
  --capability run.heartbeat \
  --capability message.publish \
  --capability message.read \
  --capability subscription.create \
  --capability subscription.read \
  --capability subscription.ack \
  --capability fact.read \
  --capability fact.write \
  --capability artifact.read \
  --capability artifact.write \
  --capability action.propose \
  --socket "$PWD/.agent-loop/canary.sock" \
  --risk-policy-file config/control-risk-policy.example.json \
  --poll-seconds 0 \
  --max-cycles 1
```

Inspect durable state:

```sh
"$CONTROL" --db "$DB" status
"$CONTROL" --db "$DB" task-list --mission "$MISSION_ID"
"$CONTROL" --db "$DB" message-list "$MISSION_ID"
"$CONTROL" --db "$DB" event-list --after-id 0 --limit 100
```

The canary crosses real boundaries: subprocess stdin/stdout, run token, Unix socket, heartbeat, typed message, artifact upload, artifact hash verification, completion gate, token revocation, and socket cleanup.

## Operator controls

```sh
# Stop new claims; current work may finish
agent-loop-control --db "$DB" mission-pause "$MISSION_ID" \
  --actor operator --reason "inspection"

# Resume claims
agent-loop-control --db "$DB" mission-resume "$MISSION_ID" --actor operator

# Cancel queued/running work and terminate the active process group
agent-loop-control --db "$DB" mission-cancel "$MISSION_ID" \
  --actor operator --reason "operator stop"
```

The service manager remains the out-of-band hard stop.

## Worker contract

The coordinator sends one JSON object to worker stdin:

```json
{
  "mission_id": "mis_example",
  "task_id": "tsk_example",
  "run_id": "run_example",
  "worker_id": "researcher-1",
  "goal": "Produce a cited report",
  "specification": {},
  "acceptance": {"minimum_artifacts": 1},
  "context": {"parent_handoffs": []},
  "workspace": "/workspaces/mis_example/tsk_example",
  "limits": {"max_runtime_seconds": 900},
  "control": {
    "socket_path": "/run/user/1000/agent-loop-researcher-1.sock",
    "token": "[ONE-RUN TOKEN]"
  }
}
```

The worker returns strict JSON on stdout:

```json
{
  "outcome": "candidate_complete",
  "summary": "Report generated and checked",
  "artifact_ids": ["art_example"],
  "evidence": [
    {"kind": "command", "value": "pytest -q", "exit_code": 0}
  ],
  "fact_proposals": [],
  "residual_risks": [],
  "requested_followups": []
}
```

Valid outcomes are `candidate_complete`, `blocked`, and `failed`. A completion candidate still fails when required evidence or artifact verification fails.

For consequential work, put a supervisor-owned command in the acceptance contract:

```json
{
  "required_evidence_kinds": ["command"],
  "minimum_artifacts": 1,
  "verification_command": ["python3", "verify_result.py"],
  "verification_timeout_seconds": 120
}
```

The verifier runs as a second bounded process under the same executable, workspace, timeout, and cancellation policy. The verifier receives a sanitized request with no run token. A nonzero verifier exit rejects completion regardless of worker-authored evidence.

## Worker communication

`agent-loop-worker` calls the scoped Unix socket. An adapter may export the run request's control values as `AGENT_LOOP_SOCKET` and `AGENT_LOOP_TOKEN` for tool-using agents.

```sh
agent-loop-worker context-get
agent-loop-worker heartbeat --lease-seconds 60
agent-loop-worker message-post \
  "mission.$MISSION_ID.general" checkpoint "draft complete"
agent-loop-worker message-list \
  --topic-prefix "mission.$MISSION_ID.general"
agent-loop-worker subscription-create \
  "mission.$MISSION_ID.research"
agent-loop-worker fact-get decision/output-format
agent-loop-worker artifact-put report.md --media-type text/markdown
agent-loop-worker action-propose external.send \
  --target-json '{"channel":"review"}' \
  --arguments-json '{"content_hash":"abc123"}' \
  --idempotency-key send-review:v1
```

Workers cannot approve or execute actions through this CLI.

## Action policy

Risk classes:

| Class | Meaning | Treatment |
|---|---|---|
| R0 | Read-only or inert | Exact capability required |
| R1 | Bounded reversible local write | Exact capability required |
| R2 | Host or service change | Human approval required |
| R3 | External communication, publication, identity, or spending | Human approval required |
| R4 | Policy, audit, persistence, privilege, or shutdown mutation | Denied |

Policy example: [`config/control-risk-policy.example.json`](config/control-risk-policy.example.json).

A proposal is inert data. An approved proposal is still inert until supervisor-owned code invokes a registered action handler. Verification reads back the target before issuing a `verified` receipt.

Mission budgets using the `runs` unit are charged atomically before process start. An exhausted configured run budget prevents the worker process from starting. Provider adapters remain responsible for token, dollar, and external-request reservations around actual usage.

## Python API

```python
from agent_loop import (
    ActionBroker,
    ArtifactStore,
    Coordinator,
    JsonSubprocessRunner,
    MessageBoard,
    SQLiteStore,
    WorkflowService,
)

store = SQLiteStore(".agent-loop/control.db")
workflow = WorkflowService(store)
board = MessageBoard(store)
artifacts = ArtifactStore(store, ".agent-loop/artifacts")
broker = ActionBroker(store, risk_policy={"workspace.write": "R1"})

mission = workflow.create_mission(
    "Produce verified output",
    "operator",
    state="active",
    idempotency_key="mission:v1",
)
```

Provider and runtime adapters should import detailed record types from the defining modules rather than treating the package root as a dump of every implementation class.

## Original closed-loop harness

The compact evaluator loop remains available:

```text
goal → worker → evaluator → feedback/history → retry → stop
```

Run the repository quality-gate example:

```sh
.venv/bin/agent-loop examples/quality_gate_loop.json
```

A loop spec is trusted executable configuration. Commands remain arrays, use `shell=False`, and can be constrained with executable allowlists, output caps, redaction, timeouts, cwd/env controls, and optional Docker/Podman wrapping.

## Runner-specific adapters

Runner-specific modules are optional adapters and are not imported by the default package namespace. The existing `agent_loop.autonomy` and `agent_loop.hermes_control` modules remain available for installations using those integrations, but the durable core does not depend on Hermes or any other agent runtime.

## Security

Read [SECURITY.md](SECURITY.md) before admitting untrusted workers.

Minimum rules:

1. Never mount the SQLite database, artifact root, policy files, or service definitions into a worker sandbox.
2. Run untrusted workers under a separate OS identity or container boundary.
3. Default network egress to none; admit only required model endpoints or proxies.
4. Treat interpreters as powerful executables even when allowlisted.
5. Keep provider secrets out of task JSON, messages, facts, artifacts, and action payloads.
6. Register action handlers only from supervisor-owned code.
7. Inspect `unknown` action outcomes before considering another attempt.
8. Keep process supervision and shutdown outside worker control.

The hardened example unit is [`deploy/systemd/agent-loop-worker@.service`](deploy/systemd/agent-loop-worker@.service).

## Quality gate

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src examples
.venv/bin/python -m build
```

The test suite uses real SQLite transactions, multiprocessing claim races, Unix sockets, subprocess groups, timeouts, cancellation, file integrity checks, installed CLI entrypoints, and end-to-end daemon canaries.

## Repository map

```text
src/agent_loop/
├── persistence.py       # SQLite, events, transactions
├── workflow.py          # missions, DAGs, claims, leases, recovery
├── message_board.py     # messages, subscriptions, facts
├── artifacts.py         # content-addressed immutable outputs
├── action_broker.py     # capabilities, approvals, budgets, receipts
├── runner_adapter.py    # bounded JSON subprocess contract
├── coordinator.py       # claim → run → validate → complete
├── worker_api.py        # run tokens and Unix socket API
├── control_cli.py       # operator/supervisor command surface
├── worker_cli.py        # scoped agent command surface
├── engine.py            # original pure feedback loop
└── command_runner.py    # original loop subprocess adapter
```

## Contributing

Keep boundaries explicit:

- tests before behavior changes
- no shell strings or `shell=True`
- no model-specific logic in durable state services
- no direct worker database access
- no worker approval or audit-write methods
- no “exactly once” claims without a transactional external target
- real boundary tests for concurrency, processes, sockets, and filesystems

Run the complete quality gate before opening a pull request.

## License

MIT. See [LICENSE](LICENSE).

## Contact

Tyler Delano — [GitHub](https://github.com/tylerdotai)

Project: [github.com/tylerdotai/agent-loop-system](https://github.com/tylerdotai/agent-loop-system)

[license-shield]: https://img.shields.io/github/license/tylerdotai/agent-loop-system.svg?style=for-the-badge
[license-url]: https://github.com/tylerdotai/agent-loop-system/blob/main/LICENSE
[python-shield]: https://img.shields.io/badge/python-3.11%2B-blue.svg?style=for-the-badge&logo=python&logoColor=white
[python-url]: https://www.python.org/
