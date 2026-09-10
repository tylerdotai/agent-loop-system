from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


class PolicyError(ValueError):
    """Raised when an autonomy action violates the active policy."""


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str
    model: str
    provider: str
    workspace: Path
    parent_id: str | None = None
    skills: tuple[str, ...] = ()
    root_required: bool = False


@dataclass(frozen=True)
class AutonomyPolicy:
    workspace_root: Path
    allowed_roles: frozenset[str]
    allowed_models: frozenset[str]
    allowed_providers: frozenset[str] = frozenset()
    max_active_agents: int = 8
    max_agent_depth: int = 2
    root_access: bool = False
    allowed_commands: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_active_agents < 1:
            raise ValueError("max_active_agents must be at least 1")
        if self.max_agent_depth < 0:
            raise ValueError("max_agent_depth must not be negative")


@dataclass(frozen=True)
class ControlPlaneConfig:
    policy: AutonomyPolicy
    registry_path: Path
    hermes_home: Path | None = None
    hermes_executable: str = "hermes"


def load_config(path: Path) -> ControlPlaneConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid control-plane config: {config_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("control-plane config must be a JSON object")
    if payload.get("version", 1) != 1:
        raise ValueError("unsupported control-plane config version")

    base = config_path.parent

    def required_string(name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value

    def string_set(name: str, required: bool = True) -> frozenset[str]:
        value = payload.get(name)
        if value is None and not required:
            return frozenset()
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"{name} must be an array of strings")
        return frozenset(value)

    workspace_root = (base / required_string("workspace_root")).resolve()
    registry_path = (base / required_string("registry_path")).resolve()
    hermes_home_raw = payload.get("hermes_home")
    if hermes_home_raw is not None and (not isinstance(hermes_home_raw, str) or not hermes_home_raw.strip()):
        raise ValueError("hermes_home must be a non-empty string when provided")
    hermes_executable = payload.get("hermes_executable", "hermes")
    if not isinstance(hermes_executable, str) or not hermes_executable.strip():
        raise ValueError("hermes_executable must be a non-empty string")
    max_active_agents = payload.get("max_active_agents", 8)
    max_agent_depth = payload.get("max_agent_depth", 2)
    root_access = payload.get("root_access", False)
    if isinstance(max_active_agents, bool) or not isinstance(max_active_agents, int):
        raise ValueError("max_active_agents must be an integer")
    if isinstance(max_agent_depth, bool) or not isinstance(max_agent_depth, int):
        raise ValueError("max_agent_depth must be an integer")
    if not isinstance(root_access, bool):
        raise ValueError("root_access must be a boolean")
    return ControlPlaneConfig(
        policy=AutonomyPolicy(
            workspace_root=workspace_root,
            allowed_roles=string_set("allowed_roles"),
            allowed_models=string_set("allowed_models"),
            allowed_providers=string_set("allowed_providers", required=False),
            max_active_agents=max_active_agents,
            max_agent_depth=max_agent_depth,
            root_access=root_access,
            allowed_commands=string_set("allowed_commands", required=False),
        ),
        registry_path=registry_path,
        hermes_home=None if hermes_home_raw is None else (base / hermes_home_raw).resolve(),
        hermes_executable=hermes_executable,
    )


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    role: str
    model: str
    provider: str
    workspace: str
    parent_id: str | None
    depth: int
    skills: tuple[str, ...]
    root_access: bool
    status: str = "provisioned"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    deployed_at: str | None = None
    last_exit_code: int | None = None
    last_error: str | None = None
    verification_status: str | None = None
    verification_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["skills"] = list(self.skills)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRecord:
        try:
            values = dict(data)
            values["skills"] = tuple(values.get("skills", ()))
            return cls(**values)
        except (TypeError, KeyError) as exc:
            raise ValueError("invalid agent record") from exc


class AgentRegistry:
    """Durable registry for provisioned agents."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._records: dict[str, AgentRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload.get("agents", [])
            if not isinstance(records, list):
                raise ValueError("agents must be a list")
            loaded = [AgentRecord.from_dict(item) for item in records]
        except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid agent registry: {self.path}") from exc
        for record in loaded:
            if record.agent_id in self._records:
                raise ValueError(f"duplicate agent ID in registry: {record.agent_id}")
            self._records[record.agent_id] = record

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._records.get(agent_id)

    def all(self) -> tuple[AgentRecord, ...]:
        return tuple(self._records.values())

    def active_count(self) -> int:
        return sum(record.status not in {"failed", "retired"} for record in self._records.values())

    def register(self, record: AgentRecord) -> None:
        if record.agent_id in self._records:
            raise PolicyError(f"agent already exists: {record.agent_id}")
        self._records[record.agent_id] = record
        try:
            self._save()
        except Exception:
            self._records.pop(record.agent_id, None)
            raise

    def update(self, agent_id: str, **changes: Any) -> AgentRecord:
        current = self._require(agent_id)
        updated = replace(current, **changes)
        self._records[agent_id] = updated
        try:
            self._save()
        except Exception:
            self._records[agent_id] = current
            raise
        return updated

    def _require(self, agent_id: str) -> AgentRecord:
        record = self.get(agent_id)
        if record is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return record

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "agents": [record.to_dict() for record in self._records.values()],
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class PolicyEngine:
    def __init__(self, policy: AutonomyPolicy, registry: AgentRegistry) -> None:
        self.policy = policy
        self.registry = registry

    def validate(self, spec: AgentSpec) -> tuple[Path, int]:
        for field_name in ("agent_id", "role", "model", "provider"):
            if not getattr(spec, field_name).strip():
                raise PolicyError(f"{field_name} must not be empty")
        if spec.role not in self.policy.allowed_roles:
            raise PolicyError(f"role is not allowed: {spec.role}")
        if spec.model not in self.policy.allowed_models:
            raise PolicyError(f"model is not allowed: {spec.model}")
        if self.policy.allowed_providers and spec.provider not in self.policy.allowed_providers:
            raise PolicyError(f"provider is not allowed: {spec.provider}")
        if spec.root_required and not self.policy.root_access:
            raise PolicyError("root access is not enabled by policy")
        if self.registry.get(spec.agent_id) is not None:
            raise PolicyError(f"agent already exists: {spec.agent_id}")
        if self.registry.active_count() >= self.policy.max_active_agents:
            raise PolicyError("active agent limit reached")

        root = self.policy.workspace_root.expanduser().resolve()
        workspace = spec.workspace.expanduser().resolve()
        if workspace == root:
            raise PolicyError("workspace must be a child of the controlled root")
        try:
            workspace.relative_to(root)
        except ValueError as exc:
            raise PolicyError(f"workspace must be inside {root}") from exc
        if workspace.exists():
            raise PolicyError(f"workspace already exists: {workspace}")

        depth = 0
        if spec.parent_id is not None:
            parent = self.registry.get(spec.parent_id)
            if parent is None:
                raise PolicyError(f"parent agent does not exist: {spec.parent_id}")
            if parent.status in {"failed", "retired"}:
                raise PolicyError(f"parent agent is not active: {spec.parent_id}")
            depth = parent.depth + 1
        if depth > self.policy.max_agent_depth:
            raise PolicyError("maximum agent depth exceeded")
        return workspace, depth

    def validate_command(self, command: Sequence[str], field_name: str) -> list[str]:
        if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
            raise PolicyError(f"{field_name} must be a non-empty list of strings")
        normalized = list(command)
        if not normalized or not all(isinstance(part, str) and part for part in normalized):
            raise PolicyError(f"{field_name} must be a non-empty list of strings")
        if self.policy.allowed_commands:
            executable = normalized[0]
            if executable not in self.policy.allowed_commands and Path(executable).name not in self.policy.allowed_commands:
                raise PolicyError(f"command is not allowed: {executable}")
        return normalized


class AgentProvisioner:
    """Create an isolated, secret-free agent package and register it."""

    def __init__(self, policy: AutonomyPolicy, registry: AgentRegistry) -> None:
        self.policy = policy
        self.registry = registry
        self.policy_engine = PolicyEngine(policy, registry)

    def provision(self, spec: AgentSpec) -> AgentRecord:
        workspace, depth = self.policy_engine.validate(spec)
        root = self.policy.workspace_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{spec.agent_id}.", dir=root))
        try:
            profile = temporary / "profile"
            profile.mkdir()
            manifest = {
                "version": 1,
                "agent_id": spec.agent_id,
                "role": spec.role,
                "model": spec.model,
                "provider": spec.provider,
                "parent_id": spec.parent_id,
                "depth": depth,
                "skills": list(spec.skills),
                "capabilities": {"root": spec.root_required},
            }
            _write_json(temporary / "agent-manifest.json", manifest)
            _write_text(
                profile / "SOUL.md",
                "# Autonomous agent profile\n\n"
                f"Role: {spec.role}\n"
                f"Agent ID: {spec.agent_id}\n\n"
                "Operate only within the registered mission, workspace, and capabilities. "
                "Return structured evidence for every task.\n",
            )
            os.replace(temporary, workspace)
        except Exception:
            _remove_directory(temporary)
            raise

        record = AgentRecord(
            agent_id=spec.agent_id,
            role=spec.role,
            model=spec.model,
            provider=spec.provider,
            workspace=str(workspace),
            parent_id=spec.parent_id,
            depth=depth,
            skills=tuple(spec.skills),
            root_access=spec.root_required,
        )
        try:
            self.registry.register(record)
        except Exception:
            _remove_directory(workspace)
            raise
        return record


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class DeploymentResult:
    success: bool
    execution: ExecutionResult
    verification: VerificationResult


class AgentSupervisor:
    """Run registered workers with bounded, shell-free subprocesses."""

    def __init__(self, policy: AutonomyPolicy, registry: AgentRegistry) -> None:
        self.registry = registry
        self.policy_engine = PolicyEngine(policy, registry)

    def run(self, agent_id: str, command: Sequence[str], timeout_seconds: float = 120) -> ExecutionResult:
        normalized = self.policy_engine.validate_command(command, "worker_command")
        record = self._active_record(agent_id)
        _validate_timeout(timeout_seconds)
        self.registry.update(agent_id, status="running", last_error=None)
        result = _run_process(normalized, Path(record.workspace), timeout_seconds)
        if result.timed_out:
            self.registry.update(agent_id, status="failed", last_exit_code=None, last_error="worker timed out")
        elif result.returncode != 0:
            self.registry.update(
                agent_id,
                status="failed",
                last_exit_code=result.returncode,
                last_error=f"worker exited with code {result.returncode}",
            )
        else:
            self.registry.update(agent_id, status="provisioned", last_exit_code=0, last_error=None)
        return result

    def _active_record(self, agent_id: str) -> AgentRecord:
        record = self.registry.get(agent_id)
        if record is None:
            raise PolicyError(f"unknown agent: {agent_id}")
        if record.status in {"failed", "retired"}:
            raise PolicyError(f"agent is {record.status}: {agent_id}")
        return record


class DeploymentVerifier:
    """Run an independent verification command in the worker workspace."""

    def __init__(self, policy: AutonomyPolicy, registry: AgentRegistry) -> None:
        self.policy_engine = PolicyEngine(policy, registry)

    def verify(self, workspace: str, command: Sequence[str], timeout_seconds: float = 120) -> VerificationResult:
        normalized = self.policy_engine.validate_command(command, "verifier_command")
        _validate_timeout(timeout_seconds)
        result = _run_process(normalized, Path(workspace), timeout_seconds)
        return VerificationResult(
            passed=not result.timed_out and result.returncode == 0,
            returncode=None if result.timed_out else result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )


class AutonomyController:
    """Coordinate hire, deploy, verify, and retire transitions."""

    def __init__(self, policy: AutonomyPolicy, registry: AgentRegistry) -> None:
        self.registry = registry
        self.policy_engine = PolicyEngine(policy, registry)
        self.supervisor = AgentSupervisor(policy, registry)
        self.verifier = DeploymentVerifier(policy, registry)

    def deploy(
        self,
        agent_id: str,
        worker_command: Sequence[str],
        verifier_command: Sequence[str],
        timeout_seconds: float = 120,
    ) -> DeploymentResult:
        self.policy_engine.validate_command(worker_command, "worker_command")
        self.policy_engine.validate_command(verifier_command, "verifier_command")
        _validate_timeout(timeout_seconds)
        record = self.registry.get(agent_id)
        if record is None:
            raise PolicyError(f"unknown agent: {agent_id}")
        execution = self.supervisor.run(agent_id, worker_command, timeout_seconds)
        if execution.timed_out or execution.returncode != 0:
            verification = VerificationResult(False, None, stderr="worker execution failed; verifier was not run")
            self.registry.update(agent_id, verification_status="not_run", verification_at=_now())
            return DeploymentResult(False, execution, verification)

        verification = self.verifier.verify(record.workspace, verifier_command, timeout_seconds)
        if not verification.passed:
            self.registry.update(
                agent_id,
                status="failed",
                last_exit_code=execution.returncode,
                last_error="independent verification failed",
                verification_status="failed",
                verification_at=_now(),
            )
            return DeploymentResult(False, execution, verification)

        self.registry.update(
            agent_id,
            status="deployed",
            deployed_at=_now(),
            last_exit_code=execution.returncode,
            last_error=None,
            verification_status="passed",
            verification_at=_now(),
        )
        return DeploymentResult(True, execution, verification)

    def retire(self, agent_id: str) -> AgentRecord:
        record = self.registry.get(agent_id)
        if record is None:
            raise PolicyError(f"unknown agent: {agent_id}")
        if record.status == "retired":
            return record
        return self.registry.update(agent_id, status="retired", last_error=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_timeout(timeout_seconds: float) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")


def _run_process(command: Sequence[str], cwd: Path, timeout_seconds: float) -> ExecutionResult:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        return ExecutionResult(-1, stderr=f"process launch failed: {type(exc).__name__}")
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        return ExecutionResult(-1, _tail(stdout), _tail(stderr), timed_out=True)
    return ExecutionResult(process.returncode, _tail(stdout), _tail(stderr))


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _remove_directory(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            _remove_directory(child)
        else:
            child.unlink()
    path.rmdir()


def _tail(value: str | None, limit: int = 12_000) -> str:
    text = value or ""
    return text if len(text) <= limit else text[-limit:]
