# Operations

This guide operates the single-host control plane. Commands assume an editable installation in a project virtual environment.

## Install

```sh
cd agent-loop-system
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Verify all four installed entrypoints. `agent-autonomy` is the compatibility CLI for the optional runtime-specific adapter; the other three are the runner-agnostic core and worker surfaces:

```sh
.venv/bin/agent-loop --help
.venv/bin/agent-loop-control --help
.venv/bin/agent-loop-worker --help
.venv/bin/agent-autonomy --help
```

## Runtime directories

Use separate directories for trusted state and disposable workspaces:

```sh
mkdir -p "$HOME/.local/state/agent-loop"
mkdir -p "$HOME/.config/agent-loop"
mkdir -p "$HOME/agent-loop-workspaces"
chmod 700 "$HOME/.local/state/agent-loop"
chmod 700 "$HOME/.config/agent-loop"
chmod 700 "$HOME/agent-loop-workspaces"
cp config/control-risk-policy.example.json \
  "$HOME/.config/agent-loop/control-risk-policy.json"
chmod 600 "$HOME/.config/agent-loop/control-risk-policy.json"
```

The control database and artifact blobs are created with owner-only permissions.

Set shell variables for examples:

```sh
DB="$HOME/.local/state/agent-loop/control.db"
ARTIFACTS="$HOME/.local/state/agent-loop/artifacts"
WORKSPACES="$HOME/agent-loop-workspaces"
PYTHON="$PWD/.venv/bin/python"
CANARY="$PWD/examples/control_plane_canary_worker.py"
CONTROL="$PWD/.venv/bin/agent-loop-control"
```

## Initialize

```sh
"$CONTROL" --db "$DB" --artifacts "$ARTIFACTS" init
```

Expected output names the resolved database and artifact paths.

## Create a mission and task

Create an active mission:

```sh
MISSION_ID=$(
  "$CONTROL" --db "$DB" mission-create \
    "Run the scoped control-plane canary" \
    --actor operator \
    --active \
    --idempotency-key canary-mission:v1 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["mission_id"])'
)
```

Create a task using the installed virtualenv interpreter:

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
  "Exercise heartbeat, board, and artifact paths" \
  --assignee worker \
  --actor operator \
  --spec-json "$SPEC_JSON" \
  --acceptance-json '{"required_evidence_kinds":["command"],"minimum_artifacts":1}' \
  --max-attempts 1 \
  --max-runtime-seconds 60 \
  --idempotency-key canary-task:v1
```

Custom workers may use an installed module path or an absolute executable/script path.

## Run one canary cycle

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
  --socket "$XDG_RUNTIME_DIR/agent-loop-canary-1.sock" \
  --risk-policy-file "$HOME/.config/agent-loop/control-risk-policy.json" \
  --poll-seconds 0 \
  --max-cycles 1
```

A successful result reports `"processed": 1`. Verify durable effects:

```sh
"$CONTROL" --db "$DB" status
"$CONTROL" --db "$DB" mission-list
"$CONTROL" --db "$DB" task-list --mission "$MISSION_ID"
"$CONTROL" --db "$DB" message-list "$MISSION_ID"
"$CONTROL" --db "$DB" event-list --after-id 0 --limit 100
```

## Operator controls

Pause new claims while allowing a current process to finish:

```sh
"$CONTROL" --db "$DB" mission-pause "$MISSION_ID" \
  --actor operator \
  --reason "operator inspection"
```

Resume:

```sh
"$CONTROL" --db "$DB" mission-resume "$MISSION_ID" --actor operator
```

Cancel queued and running work:

```sh
"$CONTROL" --db "$DB" mission-cancel "$MISSION_ID" \
  --actor operator \
  --reason "operator stop"
```

Cancellation invalidates run rows and tokens, releases resource leases, and causes the coordinator to terminate the active process group during the next cancellation poll.

The service manager remains the independent hard stop:

```sh
systemctl --user stop 'agent-loop-worker@*'
```

## Message board

Operator-side post:

```sh
"$CONTROL" --db "$DB" message-post \
  "$MISSION_ID" \
  "mission.$MISSION_ID.general" \
  decision_notice \
  "Proceed with the verified design" \
  --actor operator \
  --dedupe-key operator-decision:v1
```

Workers use `agent-loop-worker`. The daemon supplies `AGENT_LOOP_SOCKET` and `AGENT_LOOP_TOKEN` only when an adapter deliberately exports those values; the built-in JSON worker receives the same values in the request's `control` object.

Examples:

```sh
agent-loop-worker context-get
agent-loop-worker message-list --topic-prefix "mission.$MISSION_ID.general"
agent-loop-worker message-post \
  "mission.$MISSION_ID.general" checkpoint "implementation started"
agent-loop-worker heartbeat --lease-seconds 60
```

## Facts and conflicts

Create a fact:

```sh
"$CONTROL" --db "$DB" fact-put "$MISSION_ID" decision/output-format \
  '{"format":"jsonl"}' \
  --actor planner \
  --expected-version 0
```

Update with the returned version. A stale version or different owner fails closed. Use a `conflict` message when another agent disputes the value.

## Actions and approvals

Grant an exact action capability:

```sh
"$CONTROL" --db "$DB" capability-grant ops-1 host.service.restart \
  --actor operator
```

Propose using the operator CLI:

```sh
ACTION_JSON=$(
  "$CONTROL" --db "$DB" action-propose "$MISSION_ID" host.service.restart \
    --actor ops-1 \
    --target-json '{"service":"example.service"}' \
    --arguments-json '{"mode":"graceful"}' \
    --idempotency-key restart-example:v1 \
    --risk-policy-json '{"host.service.restart":"R2"}'
)
printf '%s\n' "$ACTION_JSON"
```

Approve only the exact payload hash shown by `action-show`:

```sh
ACTION_ID=$(printf '%s' "$ACTION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action_id"])')
PAYLOAD_HASH=$(printf '%s' "$ACTION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["payload_hash"])')

"$CONTROL" --db "$DB" action-approve "$ACTION_ID" \
  --approver operator \
  --payload-hash "$PAYLOAD_HASH"
```

Or deny:

```sh
"$CONTROL" --db "$DB" action-deny "$ACTION_ID" \
  --approver operator \
  --reason "change window closed"
```

Approval does not execute the action. Execution requires a supervisor-owned `ActionHandler` registered in code. The worker API has no approval or execution method.

## Budgets

Configure and inspect a budget:

```sh
"$CONTROL" --db "$DB" budget-set mission "$MISSION_ID" tokens 100000 \
  --actor operator
"$CONTROL" --db "$DB" budget-show mission "$MISSION_ID" tokens
```

Call `BudgetService.reserve`, `consume`, and `release` from provider adapters around actual usage. Reservation IDs must be stable idempotency keys.

The coordinator enforces a configured mission-level `runs` budget automatically before process start. Other units, such as tokens, dollars, and external requests, remain provider-adapter responsibilities.

## systemd system service

The repository includes `deploy/systemd/agent-loop-worker@.service` as a no-network, privilege-separated system-service template. It is intentionally **not** a user service: one static identity owns canonical state and a different static identity runs untrusted workers.

Required layout:

- `/opt/agent-loop-system`: root-owned installed application, not writable by either service identity.
- `/var/lib/agent-loop`: mode `0700`, created by systemd and owned by `agent-loop-control`; contains the database and artifacts.
- `/var/lib/agent-loop-workspaces`: mode `0711`, owned by `agent-loop-control`; task leaves are owned by `agent-loop-worker`, grouped to `agent-loop-control`, and mode `0710`. The worker gets full workspace access while the supervisor gets traversal only, which `subprocess.Popen(cwd=...)` requires before the trusted `setpriv` wrapper drops identity.
- `/run/agent-loop`: mode `0711`, created by systemd; each mode-`0600` socket is chowned to `agent-loop-worker`.
- `/etc/agent-loop`: root-owned configuration directory; the policy is mode `0640`, owned by `root:agent-loop-control`, and unreadable by the worker identity.

Create locked service identities and directories before enabling the unit:

```sh
sudo useradd --system --home-dir /var/lib/agent-loop --shell /usr/sbin/nologin agent-loop-control
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin agent-loop-worker
sudo install -d -o root -g root -m 0755 /opt/agent-loop-system
sudo install -d -o agent-loop-control -g agent-loop-control -m 0711 /var/lib/agent-loop-workspaces
sudo install -d -o root -g root -m 0755 /etc/agent-loop
sudo install -o root -g agent-loop-control -m 0640 \
  config/control-risk-policy.example.json /etc/agent-loop/control-risk-policy.json
```

Install the application and virtual environment under `/opt/agent-loop-system`, owned by root. Then install and verify the unit:

```sh
sudo install -o root -g root -m 0644 \
  deploy/systemd/agent-loop-worker@.service \
  /etc/systemd/system/agent-loop-worker@.service
sudo systemd-analyze verify /etc/systemd/system/agent-loop-worker@.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent-loop-worker@worker-1.service
```

Inspect:

```sh
sudo systemctl status agent-loop-worker@worker-1.service --no-pager
sudo journalctl -u agent-loop-worker@worker-1.service -n 100 --no-pager
```

The supervisor receives only `CAP_SETUID`, `CAP_SETGID`, `CAP_CHOWN`, and `CAP_SETPCAP` inside a strict systemd sandbox. The trusted `setpriv` wrapper changes to `agent-loop-worker`, clears supplementary groups and every capability set, enables `no_new_privs`, and sets `SIGKILL` as the parent-death signal before executing the already-allowlisted worker command.

The unit also sets `Delegate=yes` and passes `--require-cgroup`. Do not combine this with `ProtectControlGroups=true`: that directive makes the delegated hierarchy read-only inside a system service. Systemd ownership restricts writes to the service's delegated subtree. Startup fails closed when cgroup v2 delegation is unavailable. A trusted launcher attaches each worker and verifier to a fresh per-run cgroup before untrusted code executes. Return, timeout, and cancellation kill all remaining descendants with `cgroup.kill` and remove the subtree.

The supplied unit allows only `AF_UNIX`; provider-backed agents need a separately reviewed proxy or network policy. Do not casually replace that boundary with unrestricted egress.

## Recovery

### Expired worker

The next coordinator pass marks the run expired, releases resources, and either retries or fails the task at the attempt ceiling.

### Restart during an action

The dedicated action executor must call `ActionBroker.recover_executing()` at startup. This converts orphaned `executing` actions to `unknown`. Worker-daemon startup deliberately does not mutate action-executor state. Inspect the actual target before deciding whether a new action is safe.

### SQLite backup

Use SQLite's online backup API:

```sh
DB="$DB" BACKUP="$HOME/.local/state/agent-loop/control.backup.db" python3 -c '
import os, sqlite3
source = sqlite3.connect(os.environ["DB"])
target = sqlite3.connect(os.environ["BACKUP"])
with target:
    source.backup(target)
target.close()
source.close()
'
chmod 600 "$HOME/.local/state/agent-loop/control.backup.db"
```

Do not copy the main database while ignoring active WAL and SHM files.

### Restore

Stop workers first, preserve the damaged state, restore the backup as a complete database file, then run:

```sh
"$CONTROL" --db "$DB" status
"$CONTROL" --db "$DB" event-list --after-id 0 --limit 20
```

Do not automatically re-execute `unknown` actions after restore.

## Upgrade gate

Before replacing a running installation:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src examples
.venv/bin/python -m build
```

Then stop worker services, take an online backup, install the package, run `agent-loop-control status`, and restart one canary worker before the full fleet.
