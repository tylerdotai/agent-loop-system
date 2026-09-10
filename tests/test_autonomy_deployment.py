from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_loop.autonomy import (
    AgentSpec,
    AgentProvisioner,
    AgentRegistry,
    AutonomyController,
    AutonomyPolicy,
    PolicyError,
)


def make_policy(tmp_path: Path, **overrides: object) -> AutonomyPolicy:
    values = {
        "workspace_root": tmp_path / "workspaces",
        "allowed_roles": frozenset({"researcher", "verifier"}),
        "allowed_models": frozenset({"MiniMax-M2.7"}),
        "max_active_agents": 2,
        "max_agent_depth": 2,
        "root_access": True,
        "allowed_commands": frozenset({Path(sys.executable).name}),
    }
    values.update(overrides)
    return AutonomyPolicy(**values)


def provisioned_controller(tmp_path: Path) -> tuple[AutonomyController, AgentRegistry]:
    policy = make_policy(tmp_path)
    registry = AgentRegistry(tmp_path / "registry.json")
    provisioner = AgentProvisioner(policy, registry)
    provisioner.provision(
        AgentSpec(
            agent_id="worker-001",
            role="researcher",
            model="MiniMax-M2.7",
            provider="minimax",
            workspace=tmp_path / "workspaces" / "worker-001",
        )
    )
    return AutonomyController(policy, registry), registry


def python_command(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def test_deploy_runs_worker_and_separate_verifier_before_marking_deployed(tmp_path: Path) -> None:
    controller, registry = provisioned_controller(tmp_path)
    worker = python_command("from pathlib import Path; Path('artifact.txt').write_text('ready')")
    verifier = python_command(
        "from pathlib import Path; p=Path('artifact.txt'); "
        "assert p.read_text() == 'ready'; Path('verified.txt').write_text('pass')"
    )

    result = controller.deploy("worker-001", worker, verifier)

    assert result.success is True
    assert result.execution.returncode == 0
    assert result.verification.passed is True
    record = registry.get("worker-001")
    assert record is not None
    assert record.status == "deployed"
    assert Path(record.workspace, "verified.txt").read_text() == "pass"


def test_failed_verification_quarantines_agent(tmp_path: Path) -> None:
    controller, registry = provisioned_controller(tmp_path)
    worker = python_command("from pathlib import Path; Path('artifact.txt').write_text('bad')")
    verifier = python_command("raise SystemExit('verification failed')")

    result = controller.deploy("worker-001", worker, verifier)

    assert result.success is False
    assert result.verification.passed is False
    record = registry.get("worker-001")
    assert record is not None
    assert record.status == "failed"
    assert "verification" in (record.last_error or "").lower()


def test_timeout_quarantines_agent(tmp_path: Path) -> None:
    controller, registry = provisioned_controller(tmp_path)
    worker = python_command("import time; time.sleep(1)")
    verifier = python_command("raise SystemExit('must not run')")

    result = controller.deploy("worker-001", worker, verifier, timeout_seconds=0.05)

    assert result.success is False
    assert result.execution.timed_out is True
    record = registry.get("worker-001")
    assert record is not None
    assert record.status == "failed"


def test_commands_are_allowlisted_and_retirement_is_idempotent(tmp_path: Path) -> None:
    controller, registry = provisioned_controller(tmp_path)
    with pytest.raises(PolicyError, match="command is not allowed"):
        controller.deploy("worker-001", ["sh", "-c", "true"], python_command("pass"))

    first = controller.retire("worker-001")
    second = controller.retire("worker-001")

    assert first.status == "retired"
    assert second.status == "retired"
    with pytest.raises(PolicyError, match="retired"):
        controller.deploy("worker-001", python_command("pass"), python_command("pass"))
