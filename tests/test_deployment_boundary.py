from __future__ import annotations

from pathlib import Path

from agent_loop.control_cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


def test_worker_commands_expose_optional_os_identity_drop() -> None:
    parser = build_parser()
    worker_run = parser.parse_args(
        [
            "--db",
            "control.db",
            "worker-run",
            "--worker-id",
            "worker-1",
            "--role",
            "worker",
            "--allow-command",
            "python",
            "--workspace-root",
            "workspaces",
            "--worker-user",
            "agent-loop-worker",
            "--require-cgroup",
        ]
    )
    daemon = parser.parse_args(
        [
            "--db",
            "control.db",
            "worker-daemon",
            "--worker-id",
            "worker-1",
            "--role",
            "worker",
            "--allow-command",
            "python",
            "--workspace-root",
            "workspaces",
            "--worker-user",
            "agent-loop-worker",
            "--require-cgroup",
            "--capability",
            "context.read",
            "--socket",
            "control.sock",
            "--socket-mode",
            "0660",
        ]
    )

    assert worker_run.worker_user == "agent-loop-worker"
    assert daemon.worker_user == "agent-loop-worker"
    assert worker_run.require_cgroup is True
    assert daemon.require_cgroup is True
    assert daemon.socket_mode == 0o660


def test_system_service_separates_control_state_from_worker_identity() -> None:
    unit = (ROOT / "deploy/systemd/agent-loop-worker@.service").read_text(encoding="utf-8")

    required = {
        "User=agent-loop-control",
        "StateDirectory=agent-loop",
        "StateDirectoryMode=0700",
        "RuntimeDirectory=agent-loop",
        "RuntimeDirectoryMode=0711",
        "ReadWritePaths=/var/lib/agent-loop /var/lib/agent-loop-workspaces /run/agent-loop",
        "CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_CHOWN CAP_SETPCAP",
        "RestrictAddressFamilies=AF_UNIX",
    }
    assert required <= set(unit.splitlines())
    assert "--worker-user agent-loop-worker" in unit
    assert "--require-cgroup" in unit
    assert "Delegate=yes" in unit.splitlines()
    assert "ProtectControlGroups=true" not in unit.splitlines()
    assert "--socket /run/agent-loop/%i.sock" in unit
    assert "%h/.local/state/agent-loop" not in unit
    assert "WantedBy=multi-user.target" in unit


def test_operations_guide_verifies_every_installed_entrypoint() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")

    for command in (
        "agent-loop --help",
        "agent-loop-control --help",
        "agent-loop-worker --help",
        "agent-autonomy --help",
        "agent-loop-model-broker --help",
    ):
        assert command in operations


def test_operations_guide_keeps_policy_unreadable_by_worker_identity() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")

    assert "sudo install -o root -g agent-loop-control -m 0640" in operations


def test_model_broker_and_audit_workers_preserve_network_boundary() -> None:
    broker = (ROOT / "deploy/systemd/agent-loop-model-broker.service").read_text(encoding="utf-8")
    worker = (ROOT / "deploy/systemd/agent-loop-audit-worker@.service").read_text(encoding="utf-8")

    assert "User=agent-loop-model" in broker
    assert "Group=agent-loop-worker" in broker
    assert "RestrictAddressFamilies=AF_UNIX AF_INET" in broker
    assert "IPAddressDeny=any" in broker
    assert "IPAddressAllow=127.0.0.1" in broker
    assert "--connection-timeout-seconds 5" in broker
    assert "User=agent-loop-control" in worker
    assert "RestrictAddressFamilies=AF_UNIX" in worker
    assert "--capability model.invoke" in worker
    assert "--socket-mode 0660" in worker
    assert "Delegate=yes" in worker
    assert "ProtectControlGroups=true" not in worker
