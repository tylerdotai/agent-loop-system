from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .engine import ActionResult, LoopEngine, LoopReport, LoopSpec

Command = Sequence[str]
SENSITIVE_ENV_KEY_PARTS = ("secret", "token", "password", "passwd", "api_key", "apikey", "key")


class Redactor:
    def __init__(self, values: Sequence[str] | None = None, patterns: Sequence[str] | None = None) -> None:
        self.values = [value for value in (values or []) if isinstance(value, str) and value]
        self.patterns = [re.compile(pattern) for pattern in (patterns or [])]

    def text(self, value: str) -> str:
        redacted = value
        for secret in self.values:
            redacted = redacted.replace(secret, "[REDACTED]")
        for pattern in self.patterns:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, tuple):
            return [self.data(item) for item in value]
        if isinstance(value, dict):
            return {self.data(key): self.data(item) for key, item in value.items()}
        return value


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
    allowed_commands: Sequence[str] | None = None,
    redact_values: Sequence[str] | None = None,
    redact_patterns: Sequence[str] | None = None,
    container: Mapping[str, Any] | None = None,
) -> LoopReport:
    work_command = validate_command(work_command, "work_command")
    eval_command = validate_command(eval_command, "eval_command")
    validate_allowed_command(work_command, allowed_commands, "work_command")
    validate_allowed_command(eval_command, allowed_commands, "eval_command")
    command_cwd = validate_cwd(command_cwd)
    command_env = validate_env(command_env)
    container_config = validate_container(container)
    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    if not isinstance(max_output_chars, int) or max_output_chars < 1:
        raise ValueError("max_output_chars must be a positive integer")

    redactor = build_redactor(command_env, redact_values, redact_patterns)

    def worker(state: dict[str, Any]) -> ActionResult:
        payload = _json_ready_state(state)
        completed = _run_json_command(
            prepare_command(work_command, container_config),
            payload,
            timeout_seconds,
            command_cwd,
            command_env,
            max_output_chars,
            redactor,
        )
        if isinstance(completed, dict):
            output = redactor.text(_tail(str(completed.get("output", "")), max_output_chars))
            metadata = completed.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("worker metadata must be an object")
            return ActionResult(output=output, metadata=redactor.data(metadata))
        return ActionResult(output=redactor.text(_tail(str(completed), max_output_chars)))

    def evaluator(result: ActionResult, state: dict[str, Any]) -> tuple[bool, str]:
        payload = {
            "state": _json_ready_state(state),
            "result": asdict(result),
        }
        completed = _run_json_command(
            prepare_command(eval_command, container_config),
            payload,
            timeout_seconds,
            command_cwd,
            command_env,
            max_output_chars,
            redactor,
        )
        if not isinstance(completed, dict):
            raise ValueError("evaluator must return a JSON object")
        if "passed" not in completed:
            raise ValueError("evaluator JSON must include 'passed'")
        if not isinstance(completed["passed"], bool):
            raise ValueError("evaluator 'passed' must be a JSON boolean")
        return completed["passed"], redactor.text(str(completed.get("message", "")))

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


def validate_allowed_command(
    command: Sequence[str], allowed_commands: Sequence[str] | None, field_name: str
) -> None:
    if allowed_commands is None:
        return
    if isinstance(allowed_commands, (str, bytes)) or not isinstance(allowed_commands, Sequence):
        raise ValueError("allowed_commands must be a list of command names or executable paths")
    allowed = {item for item in allowed_commands if isinstance(item, str) and item}
    if len(allowed) != len(allowed_commands):
        raise ValueError("allowed_commands must be a list of non-empty strings")
    executable = command[0]
    executable_name = Path(executable).name
    if executable not in allowed and executable_name not in allowed:
        raise ValueError(f"{field_name} executable '{executable}' is not allowed by policy")


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


def validate_container(container: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if container is None:
        return None
    if not isinstance(container, Mapping):
        raise ValueError("container must be a JSON object")
    image = container.get("image")
    if not isinstance(image, str) or not image:
        raise ValueError("container.image must be a non-empty string")
    runtime = container.get("runtime", "docker")
    if not isinstance(runtime, str) or not runtime:
        raise ValueError("container.runtime must be a non-empty string")
    network = container.get("network")
    if network is not None and (not isinstance(network, str) or not network):
        raise ValueError("container.network must be a non-empty string")
    workdir = container.get("workdir")
    if workdir is not None and (not isinstance(workdir, str) or not workdir):
        raise ValueError("container.workdir must be a non-empty string")
    read_only = container.get("read_only", False)
    if not isinstance(read_only, bool):
        raise ValueError("container.read_only must be a boolean")
    volumes = container.get("volumes", [])
    if not isinstance(volumes, list):
        raise ValueError("container.volumes must be a list")
    normalized_volumes = []
    for volume in volumes:
        if not isinstance(volume, Mapping):
            raise ValueError("container.volumes entries must be objects")
        source = volume.get("source")
        target = volume.get("target")
        readonly = volume.get("read_only", True)
        if not isinstance(source, str) or not source:
            raise ValueError("container volume source must be a non-empty string")
        if not isinstance(target, str) or not target:
            raise ValueError("container volume target must be a non-empty string")
        if not isinstance(readonly, bool):
            raise ValueError("container volume read_only must be a boolean")
        normalized_volumes.append({"source": source, "target": target, "read_only": readonly})
    return {
        "runtime": runtime,
        "image": image,
        "network": network,
        "workdir": workdir,
        "read_only": read_only,
        "volumes": normalized_volumes,
    }


def build_redactor(
    command_env: Mapping[str, str] | None,
    redact_values: Sequence[str] | None,
    redact_patterns: Sequence[str] | None,
) -> Redactor:
    if redact_values is not None and (
        isinstance(redact_values, (str, bytes)) or not isinstance(redact_values, Sequence)
    ):
        raise ValueError("redact_values must be a list of strings")
    if redact_patterns is not None and (
        isinstance(redact_patterns, (str, bytes)) or not isinstance(redact_patterns, Sequence)
    ):
        raise ValueError("redact_patterns must be a list of regex strings")
    values = list(redact_values or [])
    if not all(isinstance(value, str) for value in values):
        raise ValueError("redact_values must be a list of strings")
    patterns = list(redact_patterns or [])
    if not all(isinstance(pattern, str) for pattern in patterns):
        raise ValueError("redact_patterns must be a list of regex strings")
    if command_env:
        values.extend(
            value
            for key, value in command_env.items()
            if any(part in key.lower() for part in SENSITIVE_ENV_KEY_PARTS)
        )
    return Redactor(values=values, patterns=patterns)


def prepare_command(command: Sequence[str], container: Mapping[str, Any] | None) -> list[str]:
    if container is None:
        return list(command)
    wrapped = [str(container["runtime"]), "run", "--rm", "-i"]
    if container.get("network"):
        wrapped.extend(["--network", str(container["network"])])
    if container.get("read_only"):
        wrapped.append("--read-only")
    if container.get("workdir"):
        wrapped.extend(["-w", str(container["workdir"])])
    for volume in container.get("volumes", []):
        mode = "ro" if volume.get("read_only", True) else "rw"
        wrapped.extend(["-v", f"{volume['source']}:{volume['target']}:{mode}"])
    wrapped.append(str(container["image"]))
    wrapped.extend(command)
    return wrapped


def _run_json_command(
    command: Sequence[str],
    payload: dict[str, Any],
    timeout_seconds: int,
    command_cwd: str | None,
    command_env: Mapping[str, str] | None,
    max_output_chars: int,
    redactor: Redactor,
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
        output = redactor.text(_tail((completed.stderr or completed.stdout).strip(), max_output_chars))
        raise RuntimeError(f"command failed ({completed.returncode}): {output}")
    stdout = completed.stdout.strip()
    if not stdout:
        return ""
    try:
        return redactor.data(json.loads(stdout))
    except json.JSONDecodeError:
        return redactor.text(_tail(stdout, max_output_chars))


def _tail(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


def _json_ready_state(state: dict[str, Any]) -> dict[str, Any]:
    ready = dict(state)
    ready["history"] = [asdict(entry) for entry in ready.get("history", [])]
    return ready
