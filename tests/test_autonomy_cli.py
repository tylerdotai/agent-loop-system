from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_loop.autonomy_cli import main


def write_config(tmp_path: Path) -> Path:
    config = tmp_path / "autonomy-policy.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "workspace_root": "agents",
                "registry_path": "registry.json",
                "allowed_roles": ["coder"],
                "allowed_models": ["MiniMax-M2.7"],
                "allowed_providers": ["minimax"],
                "allowed_commands": [Path(sys.executable).name],
                "max_active_agents": 2,
                "max_agent_depth": 1,
                "root_access": False,
            }
        )
    )
    return config


def test_cli_hire_list_and_retire(tmp_path: Path, capsys):
    config = write_config(tmp_path)

    assert main(["hire", "--config", str(config), "--agent-id", "coder-1", "--role", "coder", "--model", "MiniMax-M2.7", "--provider", "minimax"]) == 0
    hired = json.loads(capsys.readouterr().out)
    assert hired["agent_id"] == "coder-1"
    assert hired["status"] == "provisioned"

    assert main(["list", "--config", str(config)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [record["agent_id"] for record in listed] == ["coder-1"]

    assert main(["retire", "--config", str(config), "--agent-id", "coder-1"]) == 0
    retired = json.loads(capsys.readouterr().out)
    assert retired["status"] == "retired"


def test_cli_deploy_requires_independent_verifier(tmp_path: Path, capsys):
    config = write_config(tmp_path)
    assert main(["hire", "--config", str(config), "--agent-id", "coder-1", "--role", "coder", "--model", "MiniMax-M2.7", "--provider", "minimax"]) == 0
    capsys.readouterr()

    worker = [sys.executable, "-c", "from pathlib import Path; Path('artifact.txt').write_text('ready')"]
    verifier = [sys.executable, "-c", "from pathlib import Path; assert Path('artifact.txt').read_text() == 'ready'"]
    assert main(["deploy", "--config", str(config), "--agent-id", "coder-1", "--worker-command-json", json.dumps(worker), "--verifier-command-json", json.dumps(verifier)]) == 0
    deployed = json.loads(capsys.readouterr().out)
    assert deployed["success"] is True
    assert deployed["verification"]["passed"] is True


def test_cli_rejects_unallowed_command(tmp_path: Path, capsys):
    config = write_config(tmp_path)
    assert main(["hire", "--config", str(config), "--agent-id", "coder-1", "--role", "coder", "--model", "MiniMax-M2.7", "--provider", "minimax"]) == 0
    capsys.readouterr()

    exit_code = main(["deploy", "--config", str(config), "--agent-id", "coder-1", "--worker-command-json", json.dumps(["sh", "-c", "true"]), "--verifier-command-json", json.dumps([sys.executable, "-c", "true"])])
    assert exit_code == 2
    assert "command is not allowed" in capsys.readouterr().err
