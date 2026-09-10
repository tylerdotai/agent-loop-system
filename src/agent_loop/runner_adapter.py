from __future__ import annotations

import base64
import json
import math
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .command_runner import Redactor, validate_allowed_command, validate_command


class RunnerPolicyError(PermissionError):
    """Raised before a runner starts when its execution policy is invalid."""


class RunnerProtocolError(RuntimeError):
    """Raised when a runner fails or violates the JSON result contract."""


class RunnerTimeoutError(TimeoutError):
    """Raised after the entire runner process group is terminated on timeout."""


class RunnerCancelledError(RuntimeError):
    """Raised after the control plane terminates a cancelled runner process group."""


@dataclass(frozen=True)
class RunRequest:
    mission_id: str
    task_id: str
    run_id: str
    worker_id: str
    goal: str
    specification: dict[str, Any]
    acceptance: dict[str, Any]
    context: dict[str, Any]
    workspace: str
    limits: dict[str, Any] = field(default_factory=dict)
    control: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerResult:
    outcome: str
    summary: str
    artifact_ids: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]
    fact_proposals: tuple[dict[str, Any], ...]
    residual_risks: tuple[Any, ...]
    requested_followups: tuple[dict[str, Any], ...]

    def metadata(self) -> dict[str, Any]:
        return {
            "artifact_ids": list(self.artifact_ids),
            "evidence": [dict(item) for item in self.evidence],
            "fact_proposals": [dict(item) for item in self.fact_proposals],
            "residual_risks": list(self.residual_risks),
            "requested_followups": [dict(item) for item in self.requested_followups],
        }


@dataclass(frozen=True)
class VerificationResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    def metadata(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "source": "control_plane",
        }


class JsonSubprocessRunner:
    """Run a JSON stdin/stdout worker as one bounded process group."""

    def __init__(
        self,
        *,
        allowed_commands: Sequence[str] | set[str],
        allowed_workspace_roots: Sequence[str | Path] | set[str | Path] = (),
        redact_values: Sequence[str] | set[str] = (),
        redact_patterns: Sequence[str] = (),
        max_output_chars: int = 12_000,
        run_as_user: str | None = None,
        cgroup_root: str | Path | None = "auto",
        require_cgroup: bool = False,
    ) -> None:
        if isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int) or max_output_chars < 1:
            raise ValueError("max_output_chars must be a positive integer")
        if isinstance(allowed_commands, (str, bytes)):
            raise RunnerPolicyError("allowed_commands must be a sequence of command names")
        self.allowed_commands = tuple(allowed_commands)
        if isinstance(allowed_workspace_roots, (str, bytes, Path)):
            raise RunnerPolicyError("allowed_workspace_roots must be a sequence of paths")
        self.allowed_workspace_roots = tuple(
            Path(root).expanduser().resolve() for root in allowed_workspace_roots
        )
        missing_roots = [root for root in self.allowed_workspace_roots if not root.is_dir()]
        if missing_roots:
            raise RunnerPolicyError(f"workspace root must be an existing directory: {missing_roots[0]}")
        self.redactor = Redactor(values=tuple(redact_values), patterns=tuple(redact_patterns))
        self.max_output_chars = max_output_chars
        self.worker_uid: int | None = None
        self.worker_gid: int | None = None
        self.worker_user: str | None = None
        self._setpriv: str | None = None
        if run_as_user is not None:
            if not isinstance(run_as_user, str) or not run_as_user.strip():
                raise RunnerPolicyError("run_as_user must be a non-empty account name")
            try:
                account = pwd.getpwnam(run_as_user.strip())
            except KeyError as exc:
                raise RunnerPolicyError(f"unknown worker account: {run_as_user}") from exc
            setpriv = shutil.which("setpriv")
            if setpriv is None:
                raise RunnerPolicyError("setpriv is required for worker privilege separation")
            self.worker_uid = int(account.pw_uid)
            self.worker_gid = int(account.pw_gid)
            self.worker_user = str(account.pw_name)
            self._setpriv = str(Path(setpriv).resolve())
        self.cgroup_root = self._resolve_cgroup_root(cgroup_root)
        if require_cgroup and self.cgroup_root is None:
            raise RunnerPolicyError("cgroup v2 delegation is required but unavailable")
        self.require_cgroup = require_cgroup

    def run(
        self,
        request: RunRequest,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> RunnerResult:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise RunnerPolicyError("timeout_seconds must be a positive finite number")
        try:
            normalized_command = validate_command(command, "runner command")
            validate_allowed_command(normalized_command, self.allowed_commands, "runner command")
        except ValueError as exc:
            raise RunnerPolicyError(str(exc)) from exc
        normalized_env = self._validate_env(env)
        workdir = Path(cwd or request.workspace).expanduser().resolve()
        if not workdir.is_dir():
            raise RunnerPolicyError(f"runner cwd must be an existing directory: {workdir}")
        if self.allowed_workspace_roots and not any(
            workdir == root or workdir.is_relative_to(root) for root in self.allowed_workspace_roots
        ):
            raise RunnerPolicyError(f"runner cwd is outside allowed workspace roots: {workdir}")
        payload = json.dumps(asdict(request), sort_keys=True, separators=(",", ":"))
        process_env = self._process_environment(workdir, normalized_env)
        run_cgroup = self._create_run_cgroup(request.run_id)

        try:
            process = subprocess.Popen(
                self._launch_command(normalized_command, run_cgroup),
                cwd=workdir,
                env=process_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_run_cgroup(run_cgroup)
            raise RunnerProtocolError(f"runner could not start: {type(exc).__name__}: {exc}") from exc

        deadline = time.monotonic() + timeout_seconds
        input_payload: str | None = payload
        while True:
            if cancel_requested is not None:
                try:
                    cancelled = cancel_requested()
                except Exception as exc:
                    self._close_run_cgroup(run_cgroup)
                    self._terminate_process_group(process)
                    process.communicate()
                    raise RunnerCancelledError(
                        f"runner cancellation check failed: {type(exc).__name__}"
                    ) from exc
                if cancelled:
                    self._close_run_cgroup(run_cgroup)
                    self._terminate_process_group(process)
                    stdout, stderr = process.communicate()
                    detail = self._redact_output(self._tail((stderr or stdout).strip()), request)
                    suffix = f": {detail}" if detail else ""
                    raise RunnerCancelledError(f"runner cancelled by control plane{suffix}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._close_run_cgroup(run_cgroup)
                self._terminate_process_group(process)
                stdout, stderr = process.communicate()
                detail = self._redact_output(self._tail((stderr or stdout).strip()), request)
                suffix = f": {detail}" if detail else ""
                raise RunnerTimeoutError(f"runner timed out after {timeout_seconds} seconds{suffix}")
            wait_seconds = min(remaining, 0.1) if cancel_requested is not None else remaining
            try:
                stdout, stderr = process.communicate(input_payload, timeout=wait_seconds)
                break
            except subprocess.TimeoutExpired:
                input_payload = None

        self._close_run_cgroup(run_cgroup)
        if process.returncode != 0:
            detail = self._redact_output(self._tail((stderr or stdout).strip()), request)
            raise RunnerProtocolError(f"runner failed ({process.returncode}): {detail}")
        raw = self._redact_output(self._tail(stdout.strip()), request)
        try:
            payload_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerProtocolError("runner must return a valid JSON object") from exc
        if not isinstance(payload_data, dict):
            raise RunnerProtocolError("runner must return a valid JSON object")
        return self._parse_result(payload_data)

    def _parse_result(self, payload: dict[str, Any]) -> RunnerResult:
        outcome = payload.get("outcome")
        if outcome not in {"candidate_complete", "blocked", "failed"}:
            raise RunnerProtocolError("runner outcome must be candidate_complete, blocked, or failed")
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise RunnerProtocolError("runner summary must be a non-empty string")
        artifact_ids = self._string_tuple(payload, "artifact_ids")
        evidence = self._object_tuple(payload, "evidence")
        fact_proposals = self._object_tuple(payload, "fact_proposals")
        requested_followups = self._object_tuple(payload, "requested_followups")
        residual_risks_raw = payload.get("residual_risks")
        if not isinstance(residual_risks_raw, list):
            raise RunnerProtocolError("runner residual_risks must be a list")
        for index, item in enumerate(evidence):
            if not isinstance(item.get("kind"), str) or not item["kind"]:
                raise RunnerProtocolError(f"evidence[{index}].kind must be a non-empty string")
        return RunnerResult(
            outcome=outcome,
            summary=summary.strip(),
            artifact_ids=artifact_ids,
            evidence=evidence,
            fact_proposals=fact_proposals,
            residual_risks=tuple(residual_risks_raw),
            requested_followups=requested_followups,
        )

    def run_check(
        self,
        request: RunRequest,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> VerificationResult:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise RunnerPolicyError("timeout_seconds must be a positive finite number")
        try:
            normalized_command = validate_command(command, "verification command")
            validate_allowed_command(
                normalized_command,
                self.allowed_commands,
                "verification command",
            )
        except ValueError as exc:
            raise RunnerPolicyError(str(exc)) from exc
        normalized_env = self._validate_env(env)
        workdir = Path(cwd or request.workspace).expanduser().resolve()
        if not workdir.is_dir():
            raise RunnerPolicyError(f"verification cwd must be an existing directory: {workdir}")
        if self.allowed_workspace_roots and not any(
            workdir == root or workdir.is_relative_to(root) for root in self.allowed_workspace_roots
        ):
            raise RunnerPolicyError(
                f"verification cwd is outside allowed workspace roots: {workdir}"
            )
        payload_data = asdict(request)
        payload_data["control"] = {}
        payload = json.dumps(payload_data, sort_keys=True, separators=(",", ":"))
        process_env = self._process_environment(workdir, normalized_env)
        run_cgroup = self._create_run_cgroup(request.run_id)
        try:
            process = subprocess.Popen(
                self._launch_command(normalized_command, run_cgroup),
                cwd=workdir,
                env=process_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_run_cgroup(run_cgroup)
            raise RunnerProtocolError(
                f"verification command could not start: {type(exc).__name__}: {exc}"
            ) from exc

        deadline = time.monotonic() + timeout_seconds
        input_payload: str | None = payload
        while True:
            if cancel_requested is not None:
                try:
                    cancelled = cancel_requested()
                except Exception as exc:
                    self._close_run_cgroup(run_cgroup)
                    self._terminate_process_group(process)
                    process.communicate()
                    raise RunnerCancelledError(
                        f"verification cancellation check failed: {type(exc).__name__}"
                    ) from exc
                if cancelled:
                    self._close_run_cgroup(run_cgroup)
                    self._terminate_process_group(process)
                    process.communicate()
                    raise RunnerCancelledError("verification cancelled by control plane")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._close_run_cgroup(run_cgroup)
                self._terminate_process_group(process)
                process.communicate()
                raise RunnerTimeoutError(
                    f"verification command timed out after {timeout_seconds} seconds"
                )
            wait_seconds = min(remaining, 0.1) if cancel_requested is not None else remaining
            try:
                stdout, stderr = process.communicate(input_payload, timeout=wait_seconds)
                break
            except subprocess.TimeoutExpired:
                input_payload = None
        self._close_run_cgroup(run_cgroup)
        return VerificationResult(
            command=tuple(normalized_command),
            exit_code=int(process.returncode),
            stdout=self._redact_output(self._tail(stdout.strip()), request),
            stderr=self._redact_output(self._tail(stderr.strip()), request),
        )

    @staticmethod
    def _validate_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
        if env is None:
            return None
        if not isinstance(env, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise RunnerPolicyError("runner env must contain only string keys and values")
        return dict(env)

    @staticmethod
    def _string_tuple(payload: dict[str, Any], field_name: str) -> tuple[str, ...]:
        value = payload.get(field_name)
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise RunnerProtocolError(f"runner {field_name} must be a list of non-empty strings")
        return tuple(value)

    @staticmethod
    def _object_tuple(payload: dict[str, Any], field_name: str) -> tuple[dict[str, Any], ...]:
        value = payload.get(field_name)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise RunnerProtocolError(f"runner {field_name} must be a list of objects")
        return tuple(dict(item) for item in value)

    @staticmethod
    def _resolve_cgroup_root(configured: str | Path | None) -> Path | None:
        if configured is None:
            return None
        if configured == "auto":
            if sys.platform != "linux":
                return None
            try:
                entry = next(
                    line for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")
                )
            except (OSError, StopIteration):
                return None
            root = Path("/sys/fs/cgroup") / entry[3:].lstrip("/")
        else:
            root = Path(configured).expanduser().resolve()
        if (
            not root.is_dir()
            or not (root / "cgroup.procs").is_file()
            or not (root / "cgroup.kill").is_file()
            or not os.access(root, os.W_OK)
        ):
            return None
        return root

    def _create_run_cgroup(self, run_id: str) -> Path | None:
        if self.cgroup_root is None:
            return None
        safe_run_id = "".join(character for character in run_id if character.isalnum())[:24]
        path = self.cgroup_root / f"agent-loop-{safe_run_id}-{uuid.uuid4().hex[:10]}"
        try:
            path.mkdir()
        except OSError as exc:
            if self.require_cgroup:
                raise RunnerPolicyError(
                    f"could not create required run cgroup: {type(exc).__name__}: {exc}"
                ) from exc
            return None
        return path

    @staticmethod
    def _kill_run_cgroup(path: Path) -> None:
        try:
            (path / "cgroup.kill").write_text("1", encoding="ascii")
        except FileNotFoundError:
            return

    @staticmethod
    def _remove_run_cgroup(path: Path) -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                events = (path / "cgroup.events").read_text(encoding="ascii")
            except FileNotFoundError:
                return
            if "populated 0" in events:
                try:
                    path.rmdir()
                except FileNotFoundError:
                    return
                return
            time.sleep(0.01)
        raise RunnerProtocolError(f"run cgroup remained populated after termination: {path.name}")

    def _close_run_cgroup(self, path: Path | None) -> None:
        if path is None:
            return
        self._kill_run_cgroup(path)
        self._remove_run_cgroup(path)

    def _launch_command(
        self,
        command: Sequence[str],
        run_cgroup: Path | None = None,
    ) -> tuple[str, ...]:
        normalized = tuple(command)
        if self._setpriv is not None:
            if self.worker_uid is None or self.worker_gid is None:
                raise RunnerPolicyError("worker privilege separation is not fully configured")
            normalized = (
                self._setpriv,
                f"--reuid={self.worker_uid}",
                f"--regid={self.worker_gid}",
                "--clear-groups",
                "--bounding-set=-all",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--no-new-privs",
                "--pdeathsig=SIGKILL",
                "--",
                *normalized,
            )
        if sys.platform == "linux":
            launcher = [sys.executable, "-I", "-m", "agent_loop.sandbox_exec"]
            if run_cgroup is not None:
                launcher.extend(("--cgroup", str(run_cgroup)))
            launcher.extend(("--", *normalized))
            normalized = tuple(launcher)
        return normalized

    def prepare_workspace(self, path: str | Path) -> Path:
        workspace = Path(path).expanduser().resolve()
        if not self.allowed_workspace_roots:
            if self.worker_uid is not None:
                raise RunnerPolicyError(
                    "worker privilege separation requires an allowed workspace root"
                )
            workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(workspace, 0o700)
            return workspace
        roots = [
            root
            for root in self.allowed_workspace_roots
            if workspace != root and workspace.is_relative_to(root)
        ]
        if not roots:
            raise RunnerPolicyError("task workspace must be below an allowed workspace root")
        root = max(roots, key=lambda item: len(item.parts))
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise RunnerPolicyError("workspace root must be a real directory")
        relative = workspace.relative_to(root)
        current = root
        for component in relative.parts[:-1]:
            current = current / component
            if current.exists() and (current.is_symlink() or not current.is_dir()):
                raise RunnerPolicyError("workspace ancestor must be a real directory")
            current.mkdir(mode=0o711, exist_ok=True)
            os.chmod(current, 0o711)
        if workspace.exists() and (workspace.is_symlink() or not workspace.is_dir()):
            raise RunnerPolicyError("task workspace must be a real directory")
        workspace.mkdir(mode=0o700, exist_ok=True)
        if self.worker_uid is not None and self.worker_gid is not None:
            supervisor_uid = os.geteuid()
            supervisor_gid = os.getegid()
            os.chown(workspace, supervisor_uid, supervisor_gid)
            os.chmod(workspace, 0o710)
            os.chown(workspace, self.worker_uid, supervisor_gid)
        else:
            os.chmod(workspace, 0o700)
        return workspace

    def _process_environment(
        self,
        workdir: Path,
        configured: dict[str, str] | None,
    ) -> dict[str, str]:
        environment = {
            "HOME": str(workdir),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "LC_CTYPE": "C",
            "TZ": "UTC",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        if self.worker_user is not None:
            environment["USER"] = self.worker_user
            environment["LOGNAME"] = self.worker_user
        if configured is not None:
            environment.update(configured)
        return environment

    def _tail(self, value: str) -> str:
        if len(value) <= self.max_output_chars:
            return value
        return value[-self.max_output_chars :]

    def _redact_output(self, value: str, request: RunRequest) -> str:
        redacted = self.redactor.text(value)
        token = request.control.get("token")
        if isinstance(token, str) and token:
            raw = token.encode("utf-8")
            variants = {
                token,
                raw.hex(),
                base64.b64encode(raw).decode("ascii"),
                base64.urlsafe_b64encode(raw).decode("ascii"),
                base64.b32encode(raw).decode("ascii"),
            }
            variants.update(item.rstrip("=") for item in tuple(variants) if item != token)
            minimum_fragment = max(12, min(24, len(token) // 3))
            variants.update(
                token[index : index + minimum_fragment]
                for index in range(len(token) - minimum_fragment + 1)
            )
            for secret in sorted(variants, key=len, reverse=True):
                if secret:
                    redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    @staticmethod
    def _descendant_pids(root_pid: int) -> set[int]:
        known = {root_pid}
        descendants: set[int] = set()
        for _ in range(16):
            discovered: set[int] = set()
            try:
                entries = tuple(Path("/proc").iterdir())
            except OSError:
                return descendants
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text(encoding="ascii")
                    fields = stat.rsplit(")", 1)[1].split()
                    parent_pid = int(fields[1])
                except (OSError, IndexError, ValueError):
                    continue
                pid = int(entry.name)
                if parent_pid in known and pid not in known:
                    discovered.add(pid)
            if not discovered:
                break
            for pid in discovered:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except ProcessLookupError:
                    continue
            known.update(discovered)
            descendants.update(discovered)
        return descendants

    @classmethod
    def _terminate_process_group(cls, process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGSTOP)
        except ProcessLookupError:
            return
        descendants = cls._descendant_pids(process.pid)
        for pid in descendants:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
