from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .autonomy import (
    AgentProvisioner,
    AgentRecord,
    AgentRegistry,
    AgentSpec,
    AutonomyPolicy,
    PolicyEngine,
    PolicyError,
)


_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class HermesProfileProvisioner:
    """Provision a real Hermes profile after local policy validation."""

    def __init__(
        self,
        policy: AutonomyPolicy,
        registry: AgentRegistry,
        hermes_home: Path,
        hermes_executable: str | Path = "hermes",
        timeout_seconds: float = 120,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.hermes_home = Path(hermes_home).expanduser().resolve()
        self.hermes_executable = str(hermes_executable)
        self.timeout_seconds = timeout_seconds
        self.policy_engine = PolicyEngine(policy, registry)

    def provision(self, spec: AgentSpec) -> AgentRecord:
        self._validate_profile_name(spec.agent_id)
        profile_path = self.hermes_home / "profiles" / spec.agent_id
        if profile_path.exists():
            raise PolicyError(f"Hermes profile already exists: {spec.agent_id}")
        record = AgentProvisioner(self.policy, self.registry).provision(spec)
        try:
            self.hermes_home.mkdir(parents=True, exist_ok=True)
            self._run_hermes(
                [
                    "profile",
                    "create",
                    spec.agent_id,
                    "--no-skills",
                    "--no-alias",
                    "--description",
                    f"Autonomous {spec.role} worker",
                ]
            )
            if not profile_path.is_dir():
                raise RuntimeError(f"Hermes did not create profile: {spec.agent_id}")
            self._run_hermes(["-p", spec.agent_id, "config", "set", "model.default", spec.model])
            self._run_hermes(["-p", spec.agent_id, "config", "set", "model.provider", spec.provider])
            actual_model = self._run_hermes(["-p", spec.agent_id, "config", "get", "model.default"]).strip()
            actual_provider = self._run_hermes(["-p", spec.agent_id, "config", "get", "model.provider"]).strip()
            if actual_model != spec.model or actual_provider != spec.provider:
                raise RuntimeError("Hermes profile model/provider readback mismatch")
            self._write_profile_files(profile_path, spec)
            for skill in spec.skills:
                self._install_skill(profile_path, spec.agent_id, skill)
        except Exception:
            self.registry.update(
                spec.agent_id,
                status="failed",
                last_error="Hermes profile provisioning failed",
            )
            raise
        return record

    def _write_profile_files(self, profile_path: Path, spec: AgentSpec) -> None:
        (profile_path / "SOUL.md").write_text(
            "# Autonomous Hermes worker\n\n"
            f"Role: {spec.role}\n"
            f"Agent ID: {spec.agent_id}\n"
            f"Model: {spec.model}\n"
            f"Provider: {spec.provider}\n\n"
            "Operate only within the registered mission, workspace, and capabilities. "
            "Return structured evidence for every task.\n",
            encoding="utf-8",
        )
        (profile_path / "IDENTITY.md").write_text(
            f"# {spec.agent_id}\n\n"
            f"Role: {spec.role}\n"
            "Lifecycle: provisioned by the autonomy control plane.\n",
            encoding="utf-8",
        )

    def _install_skill(self, profile_path: Path, profile_name: str, skill: str) -> None:
        skill_root = self.hermes_home / "skills"
        candidates = [path.parent for path in skill_root.rglob("SKILL.md") if path.parent.name == skill]
        if len(candidates) > 1:
            raise PolicyError(f"local skill is ambiguous: {skill}")
        if candidates:
            relative = candidates[0].relative_to(skill_root)
            destination = profile_path / "skills" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(candidates[0], destination)
            return
        self._run_hermes(["-p", profile_name, "skills", "install", skill, "--yes"])

    def _validate_profile_name(self, name: str) -> None:
        if not _PROFILE_NAME.fullmatch(name):
            raise PolicyError("profile name must be lowercase alphanumeric with hyphens")

    def _run_hermes(self, args: Sequence[str]) -> str:
        command = self.policy_engine.validate_command([self.hermes_executable, *args], "hermes_command")
        completed = _run_subprocess(command, self.hermes_home, self.timeout_seconds, self.hermes_home)
        if completed.returncode != 0:
            raise RuntimeError(f"Hermes command failed with exit code {completed.returncode}")
        return completed.stdout


class HermesKanbanAdapter:
    """Create durable tasks through Hermes' native SQLite Kanban board."""

    def __init__(
        self,
        policy: AutonomyPolicy,
        registry: AgentRegistry,
        hermes_executable: str | Path = "hermes",
        hermes_home: Path | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        self.policy_engine = PolicyEngine(policy, registry)
        self.hermes_executable = str(hermes_executable)
        self.hermes_home = None if hermes_home is None else Path(hermes_home).expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    def create(
        self,
        title: str,
        body: str,
        assignee: str,
        parent_ids: Sequence[str] = (),
        workspace: str = "scratch",
        created_by: str = "ranger",
        board: str | None = None,
    ) -> dict[str, Any]:
        if not title.strip() or not assignee.strip():
            raise ValueError("title and assignee must not be empty")
        command = [self.hermes_executable, "kanban"]
        if board:
            command.extend(["--board", board])
        command.extend(
            [
                "create",
                title,
                "--body",
                body,
                "--assignee",
                assignee,
                "--workspace",
                workspace,
                "--created-by",
                created_by,
                "--json",
            ]
        )
        for parent_id in parent_ids:
            command.extend(["--parent", parent_id])
        normalized = self.policy_engine.validate_command(command, "hermes_command")
        completed = _run_subprocess(normalized, None, self.timeout_seconds, self.hermes_home)
        if completed.returncode != 0:
            raise RuntimeError(f"Kanban command failed with exit code {completed.returncode}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Kanban command returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Kanban command returned a non-object JSON value")
        return payload


def _run_subprocess(
    command: Sequence[str],
    cwd: Path | None,
    timeout_seconds: float,
    hermes_home: Path | None,
) -> subprocess.CompletedProcess[str]:
    env = None
    if hermes_home is not None:
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
    try:
        return subprocess.run(
            list(command),
            cwd=None if cwd is None else str(cwd),
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Hermes command timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"Hermes command could not start: {type(exc).__name__}") from exc
