from __future__ import annotations

import argparse
import json
import math
import pwd
import signal
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .model_broker import ModelBroker, ModelBrokerPolicy, ModelBrokerSocketServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the credential-isolated local model broker.")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--worker-user", required=True)
    parser.add_argument("--control-socket-root", required=True)
    parser.add_argument("--audit-log", required=True)
    parser.add_argument("--max-tokens", type=int, default=2_048)
    parser.add_argument("--max-prompt-chars", type=int, default=24_000)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--connection-timeout-seconds", type=float, default=5)
    parser.add_argument("--max-concurrent", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        account = pwd.getpwnam(args.worker_user)
        policy = ModelBrokerPolicy(
            model=args.model,
            endpoint=args.endpoint,
            worker_uid=account.pw_uid,
            control_socket_root=Path(args.control_socket_root),
            max_tokens=args.max_tokens,
            max_prompt_chars=args.max_prompt_chars,
            timeout_seconds=args.timeout_seconds,
            connection_timeout_seconds=args.connection_timeout_seconds,
            max_concurrent=args.max_concurrent,
        )
        _probe_provider(policy)
        broker = ModelBroker(policy, audit_log=args.audit_log)
        stop = threading.Event()
        previous_handlers: dict[int, Any] = {}
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, lambda _signum, _frame: stop.set())
        try:
            with ModelBrokerSocketServer(args.socket, broker):
                print(
                    json.dumps(
                        {
                            "status": "ready",
                            "socket": str(Path(args.socket).expanduser().resolve()),
                            "model": policy.model,
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                stop.wait()
        finally:
            for signal_number, handler in previous_handlers.items():
                signal.signal(signal_number, handler)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"model broker error: {exc}", file=sys.stderr)
        return 2


def _probe_provider(policy: ModelBrokerPolicy) -> None:
    health_url = urllib.parse.urljoin(policy.endpoint.rstrip("/") + "/", "health")
    models_url = urllib.parse.urljoin(policy.endpoint.rstrip("/") + "/", "v1/models")
    try:
        with urllib.request.urlopen(health_url, timeout=min(5.0, policy.timeout_seconds)) as response:
            health = json.loads(response.read(policy.max_response_bytes + 1))
        with urllib.request.urlopen(models_url, timeout=min(5.0, policy.timeout_seconds)) as response:
            models = json.loads(response.read(policy.max_response_bytes + 1))
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("local model provider health probe failed") from exc
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise RuntimeError("local model provider is not healthy")
    identifiers = {
        item.get("id")
        for item in models.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if policy.model not in identifiers:
        raise RuntimeError("configured model is not served by the local provider")
    if not math.isfinite(policy.timeout_seconds):
        raise RuntimeError("broker timeout is invalid")


if __name__ == "__main__":
    raise SystemExit(main())
