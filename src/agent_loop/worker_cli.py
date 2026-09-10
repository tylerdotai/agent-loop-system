from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .worker_api import control_call


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scoped worker client for the local control plane.")
    parser.add_argument("--socket", help="Unix socket path; defaults to AGENT_LOOP_SOCKET")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("context-get", help="Read the current task context")

    message_post = commands.add_parser("message-post", help="Append a typed message")
    message_post.add_argument("topic")
    message_post.add_argument("kind")
    message_post.add_argument("body")
    message_post.add_argument("--subject", default="")
    message_post.add_argument("--recipient", action="append", default=[])
    message_post.add_argument("--data-json", default="{}")
    message_post.add_argument("--dedupe-key")
    message_post.add_argument("--reply-to")
    message_post.add_argument("--correlation-id")

    message_list = commands.add_parser("message-list", help="List messages in the current mission")
    message_list.add_argument("--topic-prefix")
    message_list.add_argument("--limit", type=int, default=100)

    subscription_create = commands.add_parser(
        "subscription-create", help="Create an owned durable topic cursor"
    )
    subscription_create.add_argument("topic_prefix")

    subscription_read = commands.add_parser("subscription-read", help="Read after an owned cursor")
    subscription_read.add_argument("subscription_id")
    subscription_read.add_argument("--limit", type=int, default=100)

    subscription_ack = commands.add_parser("subscription-ack", help="Advance an owned cursor")
    subscription_ack.add_argument("subscription_id")
    subscription_ack.add_argument("message_id")

    heartbeat = commands.add_parser("heartbeat", help="Extend the current run lease")
    heartbeat.add_argument("--lease-seconds", type=float, required=True)

    fact_get = commands.add_parser("fact-get", help="Read a versioned shared fact")
    fact_get.add_argument("fact_key")

    fact_put = commands.add_parser("fact-put", help="Compare-and-swap an owned shared fact")
    fact_put.add_argument("fact_key")
    fact_put.add_argument("value_json")
    fact_put.add_argument("--expected-version", type=int, required=True)

    artifact_put = commands.add_parser("artifact-put", help="Upload one artifact from disk")
    artifact_put.add_argument("path")
    artifact_put.add_argument("--media-type")
    artifact_put.add_argument("--dedupe-key")

    artifact_read = commands.add_parser("artifact-read", help="Download one artifact without overwrite")
    artifact_read.add_argument("artifact_id")
    artifact_read.add_argument("output")

    action = commands.add_parser("action-propose", help="Propose a governed side effect")
    action.add_argument("action_type")
    action.add_argument("--target-json", required=True)
    action.add_argument("--arguments-json", default="{}")
    action.add_argument("--idempotency-key", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        socket_path = args.socket or os.environ.get("AGENT_LOOP_SOCKET")
        token = os.environ.get("AGENT_LOOP_TOKEN")
        if not socket_path:
            raise ValueError("AGENT_LOOP_SOCKET or --socket is required")
        if not token:
            raise ValueError("AGENT_LOOP_TOKEN is required")

        if args.command == "context-get":
            return _emit(control_call(socket_path, token, "context.get"))
        if args.command == "message-post":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "message.publish",
                    {
                        "topic": args.topic,
                        "kind": args.kind,
                        "body": args.body,
                        "subject": args.subject,
                        "recipients": args.recipient,
                        "data": _load_object(args.data_json, "data-json"),
                        "dedupe_key": args.dedupe_key,
                        "reply_to": args.reply_to,
                        "correlation_id": args.correlation_id,
                    },
                )
            )
        if args.command == "message-list":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "message.list",
                    {"topic_prefix": args.topic_prefix, "limit": args.limit},
                )
            )
        if args.command == "subscription-create":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "subscription.create",
                    {"topic_prefix": args.topic_prefix},
                )
            )
        if args.command == "subscription-read":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "subscription.read",
                    {"subscription_id": args.subscription_id, "limit": args.limit},
                )
            )
        if args.command == "subscription-ack":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "subscription.ack",
                    {
                        "subscription_id": args.subscription_id,
                        "message_id": args.message_id,
                    },
                )
            )
        if args.command == "heartbeat":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "heartbeat",
                    {"lease_seconds": args.lease_seconds},
                )
            )
        if args.command == "fact-get":
            return _emit(
                control_call(socket_path, token, "fact.get", {"fact_key": args.fact_key})
            )
        if args.command == "fact-put":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "fact.put",
                    {
                        "fact_key": args.fact_key,
                        "value": _load_json(args.value_json, "value-json"),
                        "expected_version": args.expected_version,
                    },
                )
            )
        if args.command == "artifact-put":
            source = Path(args.path).expanduser().resolve()
            if not source.is_file():
                raise ValueError(f"artifact source is not a file: {source}")
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "artifact.put",
                    {
                        "filename": source.name,
                        "content_base64": base64.b64encode(source.read_bytes()).decode("ascii"),
                        "media_type": args.media_type,
                        "dedupe_key": args.dedupe_key,
                    },
                )
            )
        if args.command == "artifact-read":
            output = Path(args.output).expanduser().resolve()
            if output.exists():
                raise ValueError(f"refusing to overwrite existing path: {output}")
            if not output.parent.is_dir():
                raise ValueError(f"artifact output parent is not a directory: {output.parent}")
            response = control_call(
                socket_path,
                token,
                "artifact.read",
                {"artifact_id": args.artifact_id},
            )
            encoded = response.get("content_base64") if isinstance(response, dict) else None
            if not isinstance(encoded, str):
                raise ValueError("artifact response did not contain content_base64")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("artifact response contained invalid base64") from exc
            with output.open("xb") as handle:
                handle.write(content)
            os.chmod(output, 0o600)
            return _emit({"artifact_id": args.artifact_id, "output": str(output), "bytes": len(content)})
        if args.command == "action-propose":
            return _emit(
                control_call(
                    socket_path,
                    token,
                    "action.propose",
                    {
                        "action_type": args.action_type,
                        "target": _load_object(args.target_json, "target-json"),
                        "arguments": _load_object(args.arguments_json, "arguments-json"),
                        "idempotency_key": args.idempotency_key,
                    },
                )
            )
        raise ValueError(f"unknown command: {args.command}")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"worker control error: {exc}", file=sys.stderr)
        return 2


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


def _emit(payload: Any) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
