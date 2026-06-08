from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .engine import ActionResult, LoopEngine, LoopReport, LoopSpec

Command = Sequence[str]


def run_command_loop(
    *,
    goal: str,
    max_iterations: int,
    work_command: Command,
    eval_command: Command,
    timeout_seconds: int = 120,
    context: dict[str, Any] | None = None,
    command_cwd: str | None = None,
    command_env: Mapping[str, str] | None = None,
    max_output_chars: int = 12_000,
) -> LoopReport:
    work_command = validate_command(work_command, "work_command")
    eval_command = validate_command(eval_command, "eval_command")
    command_cwd = validate_cwd(command_cwd)
    command_env = validate_env(command_env)
    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    if not isinstance(max_output_chars, int) or max_output_chars < 1:
        raise ValueError("max_output_chars must be a positive integer")

    def worker(state: dict[str, Any]) -> ActionResult:
        payload = _json_ready_state(state)
        completed = _run_json_command(
            work_command, payload, timeout_seconds, command_cwd, command_env, max_output_chars
        )
        if isinstance(completed, dict):
            output = _tail(str(completed.get("output", "")), max_output_chars)
            metadata = completed.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("worker metadata must be an object")
            return ActionResult(output=output, metadata=metadata)
        return ActionResult(output=_tail(str(completed), max_output_chars))

    def evaluator(result: ActionResult, state: dict[str, Any]) -> tuple[bool, str]:
        payload = {
            "state": _json_ready_state(state),
            "result": asdict(result),
        }
        completed = _run_json_command(
            eval_command, payload, timeout_seconds, command_cwd, command_env, max_output_chars
        )
        if not isinstance(completed, dict):
            raise ValueError("evaluator must return a JSON object")
        if "passed" not in completed:
            raise ValueError("evaluator JSON must include 'passed'")
        if not isinstance(completed["passed"], bool):
            raise ValueError("evaluator 'passed' must be a JSON boolean")
        return completed["passed"], str(completed.get("message", ""))

    return LoopEngine(worker=worker, evaluator=evaluator).run(
        LoopSpec(goal=goal, max_iterations=max_iterations, context=context or {})
    )


def validate_command(command: Command, field_name: str) -> list[str]:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise ValueError(f"{field_name} must be a non-empty list of strings")
    normalized = list(command)
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty list of strings")
    if not all(isinstance(part, str) and part for part in normalized):
        raise ValueError(f"{field_name} must be a non-empty list of strings")
    return normalized


def validate_cwd(command_cwd: str | None) -> str | None:
    if command_cwd is None:
        return None
    if not isinstance(command_cwd, str) or not command_cwd:
        raise ValueError("command_cwd must be a non-empty string")
    if not Path(command_cwd).is_dir():
        raise ValueError("command_cwd must be an existing directory")
    return command_cwd


def validate_env(command_env: Mapping[str, str] | None) -> dict[str, str] | None:
    if command_env is None:
        return None
    if not isinstance(command_env, Mapping):
        raise ValueError("command_env must be an object of string keys and values")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in command_env.items()):
        raise ValueError("command_env must be an object of string keys and values")
    return dict(command_env)


def _run_json_command(
    command: Sequence[str],
    payload: dict[str, Any],
    timeout_seconds: int,
    command_cwd: str | None,
    command_env: Mapping[str, str] | None,
    max_output_chars: int,
) -> Any:
    env = None if command_env is None else {**os.environ, **command_env}
    try:
        completed = subprocess.run(
            list(command),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=command_cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"command timed out after {timeout_seconds} seconds: {command[0]}") from exc
    if completed.returncode != 0:
        output = _tail((completed.stderr or completed.stdout).strip(), max_output_chars)
        raise RuntimeError(f"command failed ({completed.returncode}): {output}")
    stdout = completed.stdout.strip()
    if not stdout:
        return ""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return _tail(stdout, max_output_chars)


def _tail(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


def _json_ready_state(state: dict[str, Any]) -> dict[str, Any]:
    ready = dict(state)
    ready["history"] = [asdict(entry) for entry in ready.get("history", [])]
    return ready
