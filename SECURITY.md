# Security Policy

## Scope

Agent Loop System executes local processes and can coordinate long-running autonomous work. Treat every worker as potentially faulty, compromised, or strategically misaligned.

The control plane reduces authority. The control plane does not make arbitrary code safe by magic.

## Supported versions

Security fixes target the current `0.2.x` line until a newer minor release replaces it.

## Reporting a vulnerability

Do not put credentials, private logs, exploit payloads, or personal data in a public issue.

Use GitHub's private vulnerability reporting or security-advisory flow for `tylerdotai/agent-loop-system` when available. Otherwise contact the repository owner through the verified GitHub profile and request a private channel.

Include:

- affected version and commit
- operating system and Python version
- minimal reproduction
- expected and observed behavior
- impact
- whether exploitation crossed a worker/control-plane boundary

## Threat model

Assume a worker may attempt to:

- claim work outside the assigned role
- replay stale credentials
- impersonate another worker
- overwrite shared state
- create covert communication channels
- read another mission's artifacts
- submit fabricated artifact IDs
- reuse idempotency keys with changed payloads
- obtain approval for one payload and execute another
- leak credentials through stdout, stderr, messages, artifacts, or action payloads
- survive timeout or cancellation through child processes
- manipulate scorers, policy, audit records, or shutdown controls
- trigger an external effect repeatedly after ambiguous failure

Assume the operator, control-plane process, SQLite database, risk policy, service manager, and registered action handlers are trusted administrative components.

## Enforced controls

### Identity and authorization

- Run tokens are random, short-lived, and stored only as SHA-256 hashes.
- A token binds one actor, mission, task, run ID, capability set, and expiration.
- Authorization rechecks that the run is still current and active.
- Mission cancellation and task recovery invalidate stale tokens without waiting for token expiration.
- The worker API derives actor, mission, task, and run identity from the token. Caller-supplied identity is ignored.

### Coordination

- Messages are typed, scoped, attributed, and append-only.
- Topic access is mission-scoped.
- Subscription cursors belong to one authenticated worker.
- Facts use owner checks and compare-and-swap versions.
- Resource keys prevent simultaneous ownership of admitted hotspots.
- Task run IDs fence stale completion and heartbeat calls.

### Execution

- Commands are arrays and run with `shell=False`.
- Executables require an operator allowlist.
- Working directories require operator-admitted roots.
- The child receives a small constructed environment rather than the supervisor's environment.
- Production workers run through a trusted `setpriv` wrapper under an OS identity that cannot traverse canonical state.
- Production invocations join a fresh delegated cgroup before worker code executes. Timeout, cancellation, verifier return, and ordinary worker return kill every remaining descendant with `cgroup.kill`.
- When cgroups are optional or unavailable, Linux fallback code freezes the process group, discovers descendants through `/proc`, freezes them recursively, and kills the frozen set. This fallback is not the production boundary.
- Output is capped before entering errors or durable task metadata.
- The dynamic one-run token, common base encodings, hexadecimal form, and sufficiently long fragments are scrubbed before output parsing or persistence.
- The active run token is rejected if a worker tries to persist it through messages, facts, artifacts, or action payloads.
- The daemon's example systemd unit restricts filesystem access, privilege gain, memory, task count, and address families.

### Artifacts

- Blob names are content hashes.
- Writes are atomic.
- Blob and database permissions are owner-only.
- Symbolic links, path escape, missing files, size mismatch, and digest mismatch are rejected.
- Completion evidence must exist and belong to the current mission and task.

### Actions

- Risk classes come from operator policy, not worker output.
- Exact capabilities are granted separately from risk policy.
- Approval binds the canonical payload hash.
- Workers may propose actions but cannot approve or execute actions through the worker API.
- Plaintext secret fields are rejected before action persistence.
- Handler and verification results receive the same secret-field check.
- Configured secret values and patterns are redacted from handler errors.
- Readback is mandatory for a verified receipt.
- Ambiguous outcomes become `unknown` and are never retried automatically.
- A restarted action executor converts orphaned `executing` rows to `unknown`.

### Audit

All state transitions append attributed events. Workers cannot call an audit-write method. Do not mount the SQLite database into a worker sandbox.

## Deployment requirements

1. Use separate locked OS identities for the control plane and untrusted workers. The supplied system unit uses `agent-loop-control` and `agent-loop-worker`.
2. Keep the database, artifact root, policy files, and service definitions outside worker workspace roots.
3. Do not mount the host home directory into a worker container.
4. Prefer an ephemeral workspace per task.
5. Set `Delegate=yes` and pass `--require-cgroup`; fail startup when cgroup v2 delegation is unavailable.
6. Use read-only base filesystems and explicit writable mounts.
7. Default network access to none. Add only the model endpoint or proxy required by the role.
8. Put provider credentials in a dedicated secret broker or process environment injected at launch. Never put secret values in task specifications, messages, facts, action payloads, or artifacts.
9. Set explicit task runtime, retry, output, budget, and concurrency ceilings.
10. Register action handlers in supervisor-owned code. Never load handler code from worker-controlled paths.
11. Keep shutdown control outside worker namespaces.
12. Back up SQLite with the online backup API, not by copying live WAL files independently.
13. Review `unknown` action outcomes manually before any retry.

## Risk classes

| Class | Meaning | Default treatment |
|---|---|---|
| R0 | Read-only or inert | Authorized with exact capability |
| R1 | Bounded, reversible local write | Authorized with exact capability |
| R2 | Host or service state change | Human approval required |
| R3 | External communication, publication, identity, or spending | Human approval required |
| R4 | Policy, audit, persistence, privilege, or shutdown mutation | Denied |

A lower class is not permission. The actor still needs the exact capability.

## Secret handling

Secret references are allowed; secret values are not.

Good:

```json
{"credential_ref": "secret://provider/researcher"}
```

Rejected:

```json
{"api_key": "[REDACTED]"}
```

The rejection list covers common secret-bearing field names such as `token`, `password`, `api_key`, `client_secret`, `private_key`, `authorization`, and `cookie`.

Redaction is defense in depth, not authorization. A secret emitted by a worker should be rotated even if logs display `[REDACTED]`.

## Known limits

- SQLite file permissions do not isolate same-user processes. Production deployment must use the supplied privilege-separated worker identity or a stronger container boundary.
- The Unix socket's `0600` mode limits other users, but possession of an active token still grants the token's capabilities.
- No finite redaction list can rule out arbitrary covert encodings by a malicious process that can use a token. Scope, expiry, revocation, privilege separation, no-network execution, and output review remain the primary controls.
- SHA-256 token hashing protects database disclosure against direct token recovery only when tokens have sufficient entropy; issued tokens do.
- `setpriv` supplies a DAC/capability boundary, not a container, seccomp profile, or per-task operating-system identity.
- Executable allowlisting does not constrain every behavior of an admitted executable. A powerful interpreter remains powerful.
- Worker-provided evidence can still contain false claims. Important tasks should use an acceptance `verification_command`, deterministic gates, and independent review.
- Append-only events are application-enforced in the single SQLite database. Strong tamper evidence requires replication or signed external audit storage.
- Network egress policy belongs to the service manager, container runtime, firewall, or proxy.
- The system cannot guarantee exactly-once external effects.
- Provider-specific agent adapters remain responsible for preventing prompt/output leakage in provider logs.

## Security regression gate

Run:

```sh
python -m pytest tests/test_control_plane_adversarial.py -q
python -m pytest -q
ruff check .
python -m compileall -q src examples
python -m build
```

Security-relevant changes require tests for the real boundary: SQLite concurrency, Unix socket calls, subprocess groups, filesystem paths, or action handlers. Mock-only proof is insufficient.
