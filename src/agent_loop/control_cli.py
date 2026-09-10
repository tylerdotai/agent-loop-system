from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .action_broker import ActionBroker, BudgetService
from .artifacts import ArtifactStore
from .coordinator import Coordinator
from .message_board import MessageBoard
from .persistence import SQLiteStore
from .runner_adapter import JsonSubprocessRunner
from .workflow import WorkflowService
from .worker_api import ControlSocketServer, RunTokenService, WorkerAPI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Durable runner-agnostic multi-agent control plane."
    )
    parser.add_argument("--db", default=".agent-loop/control.db", help="SQLite control-plane database")
    parser.add_argument(
        "--artifacts",
        help="Content-addressed artifact root (default: <database-dir>/artifacts)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Initialize the control-plane database")

    mission_create = commands.add_parser("mission-create", help="Create a durable mission")
    mission_create.add_argument("goal")
    mission_create.add_argument("--actor", required=True)
    mission_create.add_argument("--active", action="store_true")
    mission_create.add_argument("--idempotency-key")
    mission_create.add_argument("--limits-json", default="{}")

    mission_list = commands.add_parser("mission-list", help="List missions")
    mission_list.add_argument("--state")

    mission_activate = commands.add_parser("mission-activate", help="Activate a draft mission")
    mission_activate.add_argument("mission_id")
    mission_activate.add_argument("--actor", required=True)

    mission_pause = commands.add_parser("mission-pause", help="Pause new claims for a mission")
    mission_pause.add_argument("mission_id")
    mission_pause.add_argument("--actor", required=True)
    mission_pause.add_argument("--reason", required=True)

    mission_resume = commands.add_parser("mission-resume", help="Resume a paused mission")
    mission_resume.add_argument("mission_id")
    mission_resume.add_argument("--actor", required=True)

    mission_cancel = commands.add_parser("mission-cancel", help="Cancel all work in a mission")
    mission_cancel.add_argument("mission_id")
    mission_cancel.add_argument("--actor", required=True)
    mission_cancel.add_argument("--reason", required=True)

    task_create = commands.add_parser("task-create", help="Create a task in an active mission")
    task_create.add_argument("mission_id")
    task_create.add_argument("title")
    task_create.add_argument("--assignee", required=True)
    task_create.add_argument("--actor", required=True)
    task_create.add_argument("--parent", action="append", default=[])
    task_create.add_argument("--resource", action="append", default=[])
    task_create.add_argument("--spec-json", default="{}")
    task_create.add_argument("--acceptance-json", default="{}")
    task_create.add_argument("--priority", type=int, default=0)
    task_create.add_argument("--max-attempts", type=int, default=2)
    task_create.add_argument("--max-runtime-seconds", type=float, default=1_800)
    task_create.add_argument("--idempotency-key")

    task_list = commands.add_parser("task-list", help="List tasks")
    task_list.add_argument("--mission")
    task_list.add_argument("--status")

    message_post = commands.add_parser("message-post", help="Append a typed board message")
    message_post.add_argument("mission_id")
    message_post.add_argument("topic")
    message_post.add_argument("kind")
    message_post.add_argument("body")
    message_post.add_argument("--actor", required=True)
    message_post.add_argument("--task-id")
    message_post.add_argument("--subject", default="")
    message_post.add_argument("--data-json", default="{}")
    message_post.add_argument("--dedupe-key")

    message_list = commands.add_parser("message-list", help="List board messages")
    message_list.add_argument("mission_id")
    message_list.add_argument("--topic-prefix")
    message_list.add_argument("--after-sequence", type=int, default=0)
    message_list.add_argument("--limit", type=int, default=100)

    fact_put = commands.add_parser("fact-put", help="Compare-and-swap a shared fact")
    fact_put.add_argument("mission_id")
    fact_put.add_argument("fact_key")
    fact_put.add_argument("value_json")
    fact_put.add_argument("--actor", required=True)
    fact_put.add_argument("--expected-version", type=int, required=True)

    fact_get = commands.add_parser("fact-get", help="Read a shared fact")
    fact_get.add_argument("mission_id")
    fact_get.add_argument("fact_key")

    capability = commands.add_parser("capability-grant", help="Grant exact action capabilities")
    capability.add_argument("agent_id")
    capability.add_argument("capabilities", nargs="+")
    capability.add_argument("--actor", required=True)

    action_propose = commands.add_parser("action-propose", help="Create a typed action request")
    action_propose.add_argument("mission_id")
    action_propose.add_argument("action_type")
    action_propose.add_argument("--actor", required=True)
    action_propose.add_argument("--target-json", required=True)
    action_propose.add_argument("--arguments-json", default="{}")
    action_propose.add_argument("--idempotency-key", required=True)
    action_propose.add_argument("--risk-policy-json", required=True)
    action_propose.add_argument("--task-id")
    action_propose.add_argument("--run-id")

    action_approve = commands.add_parser("action-approve", help="Approve an exact action payload")
    action_approve.add_argument("action_id")
    action_approve.add_argument("--approver", required=True)
    action_approve.add_argument("--payload-hash", required=True)

    action_deny = commands.add_parser("action-deny", help="Deny a pending action")
    action_deny.add_argument("action_id")
    action_deny.add_argument("--approver", required=True)
    action_deny.add_argument("--reason", required=True)

    action_show = commands.add_parser("action-show", help="Show an action request")
    action_show.add_argument("action_id")

    budget_set = commands.add_parser("budget-set", help="Set an operator-owned budget limit")
    budget_set.add_argument("scope_type")
    budget_set.add_argument("scope_id")
    budget_set.add_argument("unit")
    budget_set.add_argument("limit", type=float)
    budget_set.add_argument("--actor", required=True)

    budget_show = commands.add_parser("budget-show", help="Show a budget")
    budget_show.add_argument("scope_type")
    budget_show.add_argument("scope_id")
    budget_show.add_argument("unit")

    event_list = commands.add_parser("event-list", help="Read append-only audit events")
    event_list.add_argument("--kind")
    event_list.add_argument("--after-id", type=int, default=0)
    event_list.add_argument("--limit", type=int, default=100)

    worker = commands.add_parser("worker-run", help="Run bounded coordinator passes")
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--role", action="append", required=True)
    worker.add_argument("--allow-command", action="append", required=True)
    worker.add_argument("--workspace-root", action="append", required=True)
    worker.add_argument("--worker-user", help="OS account used for the spawned worker")
    worker.add_argument("--require-cgroup", action="store_true")
    worker.add_argument("--redact-value", action="append", default=[])
    worker.add_argument("--max-tasks", type=int, default=1)
    worker.add_argument("--lease-seconds", type=float, default=900)

    daemon = commands.add_parser("worker-daemon", help="Run a long-lived scoped worker coordinator")
    daemon.add_argument("--worker-id", required=True)
    daemon.add_argument("--role", action="append", required=True)
    daemon.add_argument("--allow-command", action="append", required=True)
    daemon.add_argument("--workspace-root", action="append", required=True)
    daemon.add_argument("--worker-user", help="OS account used for spawned workers")
    daemon.add_argument("--require-cgroup", action="store_true")
    daemon.add_argument("--redact-value", action="append", default=[])
    daemon.add_argument("--capability", action="append", required=True)
    daemon.add_argument("--socket", required=True)
    daemon.add_argument("--lease-seconds", type=float, default=900)
    daemon.add_argument("--poll-seconds", type=float, default=1)
    daemon.add_argument("--max-cycles", type=int)
    daemon.add_argument("--risk-policy-file")

    commands.add_parser("status", help="Show control-plane counts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        database = Path(args.db).expanduser().resolve()
        store = SQLiteStore(database)
        workflow = WorkflowService(store)
        board = MessageBoard(store)

        if args.command == "init":
            artifact_root = _artifact_root(args, database)
            ArtifactStore(store, artifact_root)
            ActionBroker(store, risk_policy={})
            RunTokenService(store, workflow)
            return _emit({"database": str(database), "artifacts": str(artifact_root)})

        if args.command == "mission-create":
            mission = workflow.create_mission(
                args.goal,
                args.actor,
                state="active" if args.active else "draft",
                limits=_load_object(args.limits_json, "limits-json"),
                idempotency_key=args.idempotency_key,
            )
            return _emit(asdict(mission))

        if args.command == "mission-list":
            return _emit([asdict(mission) for mission in workflow.list_missions(state=args.state)])

        if args.command == "mission-activate":
            return _emit(asdict(workflow.activate_mission(args.mission_id, args.actor)))

        if args.command == "mission-pause":
            mission = workflow.pause_mission(args.mission_id, args.actor, reason=args.reason)
            return _emit(asdict(mission))

        if args.command == "mission-resume":
            return _emit(asdict(workflow.resume_mission(args.mission_id, args.actor)))

        if args.command == "mission-cancel":
            mission = workflow.cancel_mission(args.mission_id, args.actor, reason=args.reason)
            return _emit(asdict(mission))

        if args.command == "task-create":
            task = workflow.create_task(
                args.mission_id,
                args.title,
                args.assignee,
                actor_id=args.actor,
                parents=args.parent,
                resources=args.resource,
                specification=_load_object(args.spec_json, "spec-json"),
                acceptance=_load_object(args.acceptance_json, "acceptance-json"),
                priority=args.priority,
                max_attempts=args.max_attempts,
                max_runtime_seconds=args.max_runtime_seconds,
                idempotency_key=args.idempotency_key,
            )
            return _emit(asdict(task))

        if args.command == "task-list":
            tasks = workflow.list_tasks(args.mission, status=args.status)
            return _emit([asdict(task) for task in tasks])

        if args.command == "message-post":
            message = board.publish(
                args.mission_id,
                args.topic,
                args.kind,
                args.actor,
                args.body,
                task_id=args.task_id,
                subject=args.subject,
                data=_load_object(args.data_json, "data-json"),
                dedupe_key=args.dedupe_key,
            )
            return _emit(asdict(message))

        if args.command == "message-list":
            messages = board.list_messages(
                args.mission_id,
                topic_prefix=args.topic_prefix,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
            return _emit([asdict(message) for message in messages])

        if args.command == "fact-put":
            fact = board.put_fact(
                args.mission_id,
                args.fact_key,
                _load_json(args.value_json, "value-json"),
                args.actor,
                expected_version=args.expected_version,
            )
            return _emit(asdict(fact))

        if args.command == "fact-get":
            fact = board.get_fact(args.mission_id, args.fact_key)
            return _emit(None if fact is None else asdict(fact))

        if args.command == "capability-grant":
            broker = ActionBroker(store, risk_policy={})
            broker.grant_capabilities(args.agent_id, args.capabilities, actor_id=args.actor)
            return _emit({"agent_id": args.agent_id, "capabilities": sorted(set(args.capabilities))})

        if args.command == "action-propose":
            policy = _load_string_map(args.risk_policy_json, "risk-policy-json")
            broker = ActionBroker(store, risk_policy=policy)
            action = broker.propose(
                args.actor,
                args.mission_id,
                args.action_type,
                _load_object(args.target_json, "target-json"),
                _load_object(args.arguments_json, "arguments-json"),
                task_id=args.task_id,
                run_id=args.run_id,
                idempotency_key=args.idempotency_key,
            )
            return _emit(asdict(action))

        if args.command == "action-approve":
            broker = ActionBroker(store, risk_policy={})
            action = broker.approve(
                args.action_id,
                args.approver,
                payload_hash=args.payload_hash,
            )
            return _emit(asdict(action))

        if args.command == "action-deny":
            broker = ActionBroker(store, risk_policy={})
            action = broker.deny(args.action_id, args.approver, reason=args.reason)
            return _emit(asdict(action))

        if args.command == "action-show":
            action = ActionBroker(store, risk_policy={}).get_request(args.action_id)
            return _emit(asdict(action))

        if args.command == "budget-set":
            budget = BudgetService(store).set_limit(
                args.scope_type,
                args.scope_id,
                args.unit,
                args.limit,
                actor_id=args.actor,
            )
            return _emit(asdict(budget))

        if args.command == "budget-show":
            budget = BudgetService(store).get(args.scope_type, args.scope_id, args.unit)
            return _emit(asdict(budget))

        if args.command == "event-list":
            events = store.list_events(
                kind=args.kind,
                after_id=args.after_id,
                limit=args.limit,
            )
            return _emit([asdict(event) for event in events])

        if args.command == "worker-run":
            runner = JsonSubprocessRunner(
                allowed_commands=args.allow_command,
                allowed_workspace_roots=args.workspace_root,
                redact_values=args.redact_value,
                run_as_user=args.worker_user,
                require_cgroup=args.require_cgroup,
            )
            artifacts = ArtifactStore(store, _artifact_root(args, database))
            budgets = BudgetService(store)
            coordinator = Coordinator(
                workflow,
                runner,
                worker_id=args.worker_id,
                roles=args.role,
                lease_seconds=args.lease_seconds,
                artifacts=artifacts,
                budgets=budgets,
            )
            results = coordinator.run_until_idle(max_tasks=args.max_tasks)
            return _emit([asdict(result) for result in results])

        if args.command == "worker-daemon":
            runner = JsonSubprocessRunner(
                allowed_commands=args.allow_command,
                allowed_workspace_roots=args.workspace_root,
                redact_values=args.redact_value,
                run_as_user=args.worker_user,
                require_cgroup=args.require_cgroup,
            )
            tokens = RunTokenService(store, workflow)
            artifacts = ArtifactStore(store, _artifact_root(args, database))
            budgets = BudgetService(store)
            action_broker = ActionBroker(
                store,
                risk_policy=_load_policy_file(args.risk_policy_file),
                redact_values=args.redact_value,
            )
            api = WorkerAPI(
                tokens=tokens,
                workflow=workflow,
                board=board,
                artifacts=artifacts,
                action_broker=action_broker,
            )
            capabilities = set(args.capability)
            coordinator = Coordinator(
                workflow,
                runner,
                worker_id=args.worker_id,
                roles=args.role,
                lease_seconds=args.lease_seconds,
                artifacts=artifacts,
                budgets=budgets,
                run_tokens=tokens,
                control_socket_path=args.socket,
                capabilities_by_role={role: capabilities for role in args.role},
            )
            stop = threading.Event()
            previous_handlers: dict[int, Any] = {}
            if args.max_cycles is None:
                for signal_number in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signal_number] = signal.getsignal(signal_number)
                    signal.signal(signal_number, lambda _signum, _frame: stop.set())
            try:
                with ControlSocketServer(
                    args.socket,
                    api,
                    owner_uid=runner.worker_uid,
                    owner_gid=runner.worker_gid,
                ):
                    processed = coordinator.run_daemon(
                        poll_seconds=args.poll_seconds,
                        stop_requested=stop.is_set,
                        max_cycles=args.max_cycles,
                    )
            finally:
                for signal_number, handler in previous_handlers.items():
                    signal.signal(signal_number, handler)
            return _emit(
                {
                    "processed": processed,
                    "socket": str(Path(args.socket).expanduser().resolve()),
                    "worker_id": args.worker_id,
                }
            )

        if args.command == "status":
            return _emit(workflow.status())

        raise ValueError(f"unknown command: {args.command}")
    except (OSError, sqlite3.Error, ValueError, RuntimeError, PermissionError) as exc:
        print(f"control error: {exc}", file=sys.stderr)
        return 2


def _artifact_root(args: argparse.Namespace, database: Path) -> Path:
    if args.artifacts:
        return Path(args.artifacts).expanduser().resolve()
    return database.parent / "artifacts"


def _load_json(raw: str, field_name: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc


def _load_object(raw: str, field_name: str) -> dict[str, Any]:
    value = _load_json(raw, field_name)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _load_string_map(raw: str, field_name: str) -> dict[str, str]:
    value = _load_object(raw, field_name)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"{field_name} must map strings to strings")
    return value


def _load_policy_file(path: str | None) -> dict[str, str]:
    if path is None:
        return {}
    policy_path = Path(path).expanduser().resolve()
    try:
        raw = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"risk policy could not be read: {policy_path}") from exc
    return _load_string_map(raw, "risk policy file")


def _emit(payload: Any) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
