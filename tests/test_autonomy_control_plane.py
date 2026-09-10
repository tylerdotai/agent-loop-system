from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_loop.autonomy import (
    AgentSpec,
    AgentProvisioner,
    AgentRegistry,
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
    }
    values.update(overrides)
    return AutonomyPolicy(**values)


def make_spec(tmp_path: Path, agent_id: str = "researcher-001", **overrides: object) -> AgentSpec:
    values = {
        "agent_id": agent_id,
        "role": "researcher",
        "model": "MiniMax-M2.7",
        "provider": "minimax",
        "workspace": tmp_path / "workspaces" / agent_id,
        "skills": ("grounded-citations",),
        "root_required": True,
    }
    values.update(overrides)
    return AgentSpec(**values)


def test_provisioning_persists_isolated_manifest_without_secrets(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    registry = AgentRegistry(tmp_path / "registry.json")
    provisioner = AgentProvisioner(policy, registry)

    record = provisioner.provision(make_spec(tmp_path))

    assert record.status == "provisioned"
    assert record.depth == 0
    assert record.workspace == str(tmp_path / "workspaces" / "researcher-001")
    manifest = Path(record.workspace) / "agent-manifest.json"
    soul = Path(record.workspace) / "profile" / "SOUL.md"
    assert manifest.is_file()
    assert soul.is_file()
    manifest_data = json.loads(manifest.read_text())
    assert manifest_data["agent_id"] == "researcher-001"
    assert manifest_data["capabilities"]["root"] is True
    serialized = manifest.read_text() + soul.read_text()
    assert "MINIMAX_API_KEY" not in serialized
    assert "token" not in serialized.lower()

    reloaded = AgentRegistry(tmp_path / "registry.json")
    assert reloaded.get("researcher-001") == record


def test_policy_rejects_workspace_outside_controlled_root(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    provisioner = AgentProvisioner(policy, AgentRegistry(tmp_path / "registry.json"))

    with pytest.raises(PolicyError, match="workspace must be inside"):
        provisioner.provision(make_spec(tmp_path, workspace=tmp_path / "outside"))


def test_policy_rejects_unapproved_role_and_model(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    provisioner = AgentProvisioner(policy, AgentRegistry(tmp_path / "registry.json"))

    with pytest.raises(PolicyError, match="role is not allowed"):
        provisioner.provision(make_spec(tmp_path, role="untrusted"))

    with pytest.raises(PolicyError, match="model is not allowed"):
        provisioner.provision(make_spec(tmp_path, model="unknown-model"))


def test_policy_rejects_child_beyond_maximum_depth(tmp_path: Path) -> None:
    policy = make_policy(tmp_path, max_agent_depth=0)
    registry = AgentRegistry(tmp_path / "registry.json")
    provisioner = AgentProvisioner(policy, registry)
    provisioner.provision(make_spec(tmp_path, agent_id="parent"))

    with pytest.raises(PolicyError, match="maximum agent depth"):
        provisioner.provision(
            make_spec(
                tmp_path,
                agent_id="child",
                parent_id="parent",
                workspace=tmp_path / "workspaces" / "child",
            )
        )


def test_policy_rejects_active_agent_limit_and_duplicate_identity(tmp_path: Path) -> None:
    policy = make_policy(tmp_path, max_active_agents=1)
    registry = AgentRegistry(tmp_path / "registry.json")
    provisioner = AgentProvisioner(policy, registry)
    provisioner.provision(make_spec(tmp_path))

    with pytest.raises(PolicyError, match="active agent limit"):
        provisioner.provision(make_spec(tmp_path, agent_id="researcher-002"))

    with pytest.raises(PolicyError, match="already exists"):
        provisioner.provision(make_spec(tmp_path, agent_id="researcher-001"))
