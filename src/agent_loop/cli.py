from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .command_runner import run_command_loop


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a closed agent loop from a JSON spec.")
    parser.add_argument("spec", help="Path to loop spec JSON")
    args = parser.parse_args(argv)

    try:
        spec = _load_spec(Path(args.spec))
        report = run_command_loop(
            goal=str(spec["goal"]),
            max_iterations=int(spec.get("max_iterations", 5)),
            work_command=spec["work_command"],
            eval_command=spec["eval_command"],
            timeout_seconds=int(spec.get("timeout_seconds", 120)),
            context=_load_context(spec),
            command_cwd=spec.get("command_cwd"),
            command_env=_load_command_env(spec),
            max_output_chars=int(spec.get("max_output_chars", 12_000)),
        )
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"invalid spec: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(asdict(report), indent=2))
    return 0 if report.success else 1


def _load_spec(spec_path: Path) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text())
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    return spec


def _load_context(spec: dict[str, Any]) -> dict[str, Any]:
    context = spec.get("context", {})
    if not isinstance(context, dict):
        raise ValueError("context must be a JSON object")
    return context


def _load_command_env(spec: dict[str, Any]) -> dict[str, str] | None:
    command_env = spec.get("command_env")
    if command_env is None:
        return None
    if not isinstance(command_env, dict):
        raise ValueError("command_env must be a JSON object")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in command_env.items()):
        raise ValueError("command_env must be a JSON object with string keys and values")
    return command_env


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

