from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .autonomy import (
    AgentProvisioner,
    AgentRecord,
    AgentRegistry,
    AgentSpec,
    AutonomyController,
    DeploymentResult,
    load_config,
)
from .hermes_control import HermesKanbanAdapter, HermesProfileProvisioner


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        registry = AgentRegistry(config.registry_path)
        if args.action == "hire":
            spec = AgentSpec(
                agent_id=args.agent_id,
                role=args.role,
                model=args.model,
                provider=args.provider,
                workspace=args.workspace or config.policy.workspace_root / args.agent_id,
                parent_id=args.parent_id,
                skills=tuple(args.skill),
                root_required=args.root_required,
            )
            provisioner = (
                AgentProvisioner(config.policy, registry)
                if config.hermes_home is None
                else HermesProfileProvisioner(
                    config.policy,
                    registry,
                    config.hermes_home,
                    config.hermes_executable,
                )
            )
            record = provisioner.provision(spec)
            _print(record)
            return 0
        if args.action == "list":
            _print([record.to_dict() for record in registry.all()])
            return 0
        controller = AutonomyController(config.policy, registry)
        if args.action == "deploy":
            result = controller.deploy(
                args.agent_id,
                _load_command_json(args.worker_command_json, "worker_command"),
                _load_command_json(args.verifier_command_json, "verifier_command"),
                args.timeout,
            )
            _print(result)
            return 0 if result.success else 1
        if args.action == "retire":
            _print(controller.retire(args.agent_id))
            return 0
        if args.action == "task-create":
            task = HermesKanbanAdapter(
                config.policy,
                registry,
                config.hermes_executable,
                config.hermes_home,
            ).create(
                args.title,
                args.body,
                args.assignee,
                parent_ids=tuple(args.parent),
                workspace=args.workspace,
                created_by=args.created_by,
                board=args.board,
            )
            _print(task)
            return 0
        raise ValueError(f"unknown action: {args.action}")
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        print(f"autonomy error: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate the Ranger autonomy control plane.")
    subparsers = parser.add_subparsers(dest="action", required=True)

    hire = subparsers.add_parser("hire", help="provision and register an agent")
    _add_config(hire)
    hire.add_argument("--agent-id", required=True)
    hire.add_argument("--role", required=True)
    hire.add_argument("--model", required=True)
    hire.add_argument("--provider", required=True)
    hire.add_argument("--workspace", type=Path)
    hire.add_argument("--parent-id")
    hire.add_argument("--skill", action="append", default=[])
    hire.add_argument("--root-required", action="store_true")

    listing = subparsers.add_parser("list", help="list registered agents")
    _add_config(listing)

    deploy = subparsers.add_parser("deploy", help="execute and independently verify an agent")
    _add_config(deploy)
    deploy.add_argument("--agent-id", required=True)
    deploy.add_argument("--worker-command-json", required=True, help="JSON array of executable and arguments")
    deploy.add_argument("--verifier-command-json", required=True, help="JSON array of executable and arguments")
    deploy.add_argument("--timeout", type=float, default=120)

    retire = subparsers.add_parser("retire", help="retire an agent")
    _add_config(retire)
    retire.add_argument("--agent-id", required=True)

    task_create = subparsers.add_parser("task-create", help="create a native Hermes Kanban task")
    _add_config(task_create)
    task_create.add_argument("title")
    task_create.add_argument("--body", default="")
    task_create.add_argument("--assignee", required=True)
    task_create.add_argument("--parent", action="append", default=[])
    task_create.add_argument("--workspace", default="scratch")
    task_create.add_argument("--created-by", default="ranger")
    task_create.add_argument("--board")
    return parser


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)


def _load_command_json(raw: str, field_name: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a JSON array of strings")
    return value


def _print(value: object) -> None:
    if isinstance(value, AgentRecord):
        value = value.to_dict()
    elif isinstance(value, DeploymentResult):
        value = asdict(value)
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
