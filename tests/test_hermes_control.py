from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from agent_loop.autonomy import AgentSpec, AgentRegistry, AutonomyPolicy
from agent_loop.hermes_control import HermesKanbanAdapter, HermesProfileProvisioner


def make_policy(tmp_path: Path) -> AutonomyPolicy:
    return AutonomyPolicy(
        workspace_root=tmp_path / "agents",
        allowed_roles=frozenset({"coder"}),
        allowed_models=frozenset({"MiniMax-M2.7"}),
        allowed_providers=frozenset({"minimax"}),
        allowed_commands=frozenset({"hermes", Path(sys.executable).name}),
    )


def make_fake_hermes(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "hermes.log"
    executable = tmp_path / "hermes"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log = Path(os.environ['FAKE_HERMES_LOG'])
with log.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
args = sys.argv[1:]
if args[:2] == ['profile', 'create']:
    name = args[2]
    home = Path(os.environ['HERMES_HOME'])
    (home / 'profiles' / name).mkdir(parents=True, exist_ok=True)
    print(json.dumps({'profile': name}))
elif args[:2] == ['kanban', 'create']:
    print(json.dumps({'id': 'task-1', 'title': args[2], 'status': 'todo'}))
elif len(args) >= 5 and args[:3] == ['-p', args[1], 'config'] and args[3] == 'get':
    print('MiniMax-M2.7' if args[4] == 'model.default' else 'minimax')
elif args[:3] == ['-p', args[1], 'skills']:
    print(json.dumps({'installed': args[-2]}))
else:
    print(json.dumps({'ok': True}))
""".strip()
    )
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    return executable, log


def test_profile_provisioner_creates_hermes_profile_and_installs_skills(tmp_path: Path, monkeypatch):
    executable, log = make_fake_hermes(tmp_path)
    monkeypatch.setenv("FAKE_HERMES_LOG", str(log))
    home = tmp_path / "hermes-home"
    registry = AgentRegistry(tmp_path / "registry.json")
    provisioner = HermesProfileProvisioner(make_policy(tmp_path), registry, home, executable)
    spec = AgentSpec(
        agent_id="coder-1",
        role="coder",
        model="MiniMax-M2.7",
        provider="minimax",
        workspace=tmp_path / "agents" / "coder-1",
        skills=("test-driven-development",),
    )

    record = provisioner.provision(spec)

    profile = home / "profiles" / "coder-1"
    assert record.status == "provisioned"
    assert (profile / "SOUL.md").exists()
    assert (profile / "IDENTITY.md").exists()
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0][:4] == ["profile", "create", "coder-1", "--no-skills"]
    assert ["-p", "coder-1", "skills", "install"] == next(command[:4] for command in commands if command[:4] == ["-p", "coder-1", "skills", "install"])
    assert ["-p", "coder-1", "config", "set", "model.default", "MiniMax-M2.7"] in commands
    assert ["-p", "coder-1", "config", "set", "model.provider", "minimax"] in commands


def test_kanban_adapter_creates_task_and_parses_json(tmp_path: Path, monkeypatch):
    executable, log = make_fake_hermes(tmp_path)
    monkeypatch.setenv("FAKE_HERMES_LOG", str(log))
    adapter = HermesKanbanAdapter(make_policy(tmp_path), AgentRegistry(tmp_path / "registry.json"), executable)

    task = adapter.create("Verify worker", "Run the verifier.", "coder-1", parent_ids=("parent-1",))

    assert task == {"id": "task-1", "title": "Verify worker", "status": "todo"}


def test_profile_name_rejects_path_traversal(tmp_path: Path):
    executable, _ = make_fake_hermes(tmp_path)
    provisioner = HermesProfileProvisioner(make_policy(tmp_path), AgentRegistry(tmp_path / "registry.json"), tmp_path / "hermes-home", executable)
    spec = AgentSpec("../escape", "coder", "MiniMax-M2.7", "minimax", tmp_path / "agents" / "safe")

    with pytest.raises(ValueError, match="profile name"):
        provisioner.provision(spec)
