from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_loop.action_broker import ActionBroker
from agent_loop.control_cli import main
from agent_loop.message_board import MessageBoard
from agent_loop.persistence import SQLiteStore
from agent_loop.workflow import WorkflowService


def run_cli(capsys, *args: str):
    exit_code = main(list(args))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out else None
    return exit_code, payload, captured.err


def valid_result() -> dict:
    return {
        "outcome": "candidate_complete",
        "summary": "completed from CLI",
        "artifact_ids": [],
        "evidence": [{"kind": "command", "value": "self-check", "exit_code": 0}],
        "fact_proposals": [],
        "residual_risks": [],
        "requested_followups": [],
    }


def test_cli_initializes_database_and_manages_mission_task_graph(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    common = ("--db", str(database))

    code, initialized, error = run_cli(capsys, *common, "init")
    assert code == 0
    assert error == ""
    assert initialized["database"] == str(database.resolve())

    code, mission, _ = run_cli(
        capsys,
        *common,
        "mission-create",
        "Build a report",
        "--actor",
        "operator",
        "--active",
        "--idempotency-key",
        "report:v1",
    )
    assert code == 0
    assert mission["state"] == "active"

    code, parent, _ = run_cli(
        capsys,
        *common,
        "task-create",
        mission["mission_id"],
        "Research",
        "--assignee",
        "researcher",
        "--actor",
        "planner",
        "--spec-json",
        "{}",
        "--acceptance-json",
        "{}",
    )
    assert code == 0
    code, child, _ = run_cli(
        capsys,
        *common,
        "task-create",
        mission["mission_id"],
        "Write",
        "--assignee",
        "writer",
        "--actor",
        "planner",
        "--parent",
        parent["task_id"],
        "--resource",
        "file:report.md",
    )
    assert code == 0
    assert child["status"] == "blocked"

    code, listed, _ = run_cli(capsys, *common, "task-list", "--mission", mission["mission_id"])
    assert code == 0
    assert [item["title"] for item in listed] == ["Research", "Write"]


def test_cli_posts_messages_and_updates_versioned_facts(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    common = ("--db", str(database))
    _, mission, _ = run_cli(capsys, *common, "mission-create", "Coordinate", "--actor", "operator", "--active")
    topic = f"mission.{mission['mission_id']}.general"

    code, message, _ = run_cli(
        capsys,
        *common,
        "message-post",
        mission["mission_id"],
        topic,
        "question",
        "What changed?",
        "--actor",
        "reviewer",
        "--dedupe-key",
        "question:v1",
    )
    assert code == 0
    assert message["actor_id"] == "reviewer"

    code, listed, _ = run_cli(capsys, *common, "message-list", mission["mission_id"])
    assert code == 0
    assert listed[0]["message_id"] == message["message_id"]

    code, fact, _ = run_cli(
        capsys,
        *common,
        "fact-put",
        mission["mission_id"],
        "decision/format",
        '{"format":"jsonl"}',
        "--actor",
        "planner",
        "--expected-version",
        "0",
    )
    assert code == 0
    assert fact["version"] == 1

    code, loaded, _ = run_cli(
        capsys,
        *common,
        "fact-get",
        mission["mission_id"],
        "decision/format",
    )
    assert code == 0
    assert loaded["value"] == {"format": "jsonl"}


def test_cli_runs_bounded_worker_through_durable_coordinator(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    common = ("--db", str(database))
    _, mission, _ = run_cli(capsys, *common, "mission-create", "Execute", "--actor", "operator", "--active")
    command = [sys.executable, "-c", f"print({json.dumps(valid_result())!r})"]
    specification = json.dumps({"command": command, "timeout_seconds": 5})
    acceptance = json.dumps({"required_evidence_kinds": ["command"]})
    _, task, _ = run_cli(
        capsys,
        *common,
        "task-create",
        mission["mission_id"],
        "Execute JSON worker",
        "--assignee",
        "builder",
        "--actor",
        "planner",
        "--spec-json",
        specification,
        "--acceptance-json",
        acceptance,
    )

    code, results, error = run_cli(
        capsys,
        *common,
        "worker-run",
        "--worker-id",
        "builder-1",
        "--role",
        "builder",
        "--allow-command",
        Path(sys.executable).name,
        "--workspace-root",
        str(tmp_path),
        "--max-tasks",
        "1",
    )

    assert code == 0
    assert error == ""
    assert results[0]["status"] == "completed"
    assert WorkflowService(SQLiteStore(database)).get_task(task["task_id"]).status == "succeeded"


def test_cli_grants_capability_and_approves_exact_action_payload(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    common = ("--db", str(database))

    code, grant, _ = run_cli(
        capsys,
        *common,
        "capability-grant",
        "ops-1",
        "host.service.restart",
        "--actor",
        "operator",
    )
    assert code == 0
    assert grant == {"agent_id": "ops-1", "capabilities": ["host.service.restart"]}

    code, action, _ = run_cli(
        capsys,
        *common,
        "action-propose",
        "mission-1",
        "host.service.restart",
        "--actor",
        "ops-1",
        "--target-json",
        '{"service":"example.service"}',
        "--arguments-json",
        '{"mode":"graceful"}',
        "--idempotency-key",
        "restart:v1",
        "--risk-policy-json",
        '{"host.service.restart":"R2"}',
    )
    assert code == 0
    assert action["status"] == "awaiting_approval"

    code, approved, _ = run_cli(
        capsys,
        *common,
        "action-approve",
        action["action_id"],
        "--approver",
        "operator",
        "--payload-hash",
        action["payload_hash"],
    )
    assert code == 0
    assert approved["status"] == "authorized"


def test_cli_status_and_invalid_json_fail_cleanly(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    common = ("--db", str(database))
    run_cli(capsys, *common, "mission-create", "Status", "--actor", "operator", "--active")

    code, status, _ = run_cli(capsys, *common, "status")
    assert code == 0
    assert status["missions"] == {"active": 1}
    assert status["tasks"] == {}
    assert status["events"] >= 1

    code = main([*common, "fact-put", "mission-1", "bad", "not-json", "--actor", "planner", "--expected-version", "0"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "control error" in captured.err
    assert "Traceback" not in captured.err


def test_cli_daemon_runs_scoped_worker_api_for_one_canary_cycle(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    socket_path = tmp_path / "control.sock"
    common = ("--db", str(database), "--artifacts", str(tmp_path / "artifacts"))
    _, mission, _ = run_cli(
        capsys,
        *common,
        "mission-create",
        "Daemon canary",
        "--actor",
        "operator",
        "--active",
    )
    source = """
import json
import socket
import sys

request = json.load(sys.stdin)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect(request["control"]["socket_path"])
    client.sendall(json.dumps({
        "token": request["control"]["token"],
        "method": "message.publish",
        "params": {
            "topic": f"mission.{request['mission_id']}.general",
            "kind": "checkpoint",
            "body": "daemon canary",
        },
    }).encode() + b"\\n")
    response = b""
    while not response.endswith(b"\\n"):
        response += client.recv(65536)
assert json.loads(response)["ok"] is True
print(json.dumps({
    "outcome": "candidate_complete",
    "summary": "daemon completed",
    "artifact_ids": [],
    "evidence": [{"kind": "command", "value": "daemon canary", "exit_code": 0}],
    "fact_proposals": [],
    "residual_risks": [],
    "requested_followups": [],
}))
"""
    specification = json.dumps(
        {"command": [sys.executable, "-c", source], "cwd": str(tmp_path), "timeout_seconds": 5}
    )
    run_cli(
        capsys,
        *common,
        "task-create",
        mission["mission_id"],
        "Run daemon canary",
        "--assignee",
        "worker",
        "--actor",
        "planner",
        "--spec-json",
        specification,
        "--acceptance-json",
        '{"required_evidence_kinds":["command"]}',
    )

    code, result, error = run_cli(
        capsys,
        *common,
        "worker-daemon",
        "--worker-id",
        "worker-1",
        "--role",
        "worker",
        "--allow-command",
        Path(sys.executable).name,
        "--workspace-root",
        str(tmp_path),
        "--capability",
        "message.publish",
        "--socket",
        str(socket_path),
        "--max-cycles",
        "1",
        "--poll-seconds",
        "0",
    )

    assert code == 0
    assert error == ""
    assert result["processed"] == 1
    assert not socket_path.exists()
    messages = MessageBoard(SQLiteStore(database)).list_messages(mission["mission_id"])
    assert [message.body for message in messages] == ["daemon canary"]


def test_cli_exposes_operator_mission_lifecycle_and_event_readback(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    common = ("--db", str(database))
    _, mission, _ = run_cli(
        capsys,
        *common,
        "mission-create",
        "Operator lifecycle",
        "--actor",
        "operator",
    )

    _, active, _ = run_cli(
        capsys, *common, "mission-activate", mission["mission_id"], "--actor", "operator"
    )
    _, paused, _ = run_cli(
        capsys,
        *common,
        "mission-pause",
        mission["mission_id"],
        "--actor",
        "operator",
        "--reason",
        "inspection",
    )
    _, resumed, _ = run_cli(
        capsys, *common, "mission-resume", mission["mission_id"], "--actor", "operator"
    )
    _, cancelled, _ = run_cli(
        capsys,
        *common,
        "mission-cancel",
        mission["mission_id"],
        "--actor",
        "operator",
        "--reason",
        "stop",
    )
    code, missions, error = run_cli(capsys, *common, "mission-list", "--state", "cancelled")
    _, events, _ = run_cli(capsys, *common, "event-list", "--kind", "mission.cancelled")

    assert active["state"] == "active"
    assert paused["state"] == "paused"
    assert resumed["state"] == "active"
    assert cancelled["state"] == "cancelled"
    assert code == 0
    assert error == ""
    assert [item["mission_id"] for item in missions] == [mission["mission_id"]]
    assert events[0]["payload"]["reason"] == "stop"


def test_cli_denies_action_and_configures_budget_with_readback(tmp_path: Path, capsys) -> None:
    database = tmp_path / "control.db"
    common = ("--db", str(database))
    run_cli(
        capsys,
        *common,
        "capability-grant",
        "ops-1",
        "host.service.restart",
        "--actor",
        "operator",
    )
    _, action, _ = run_cli(
        capsys,
        *common,
        "action-propose",
        "mission-1",
        "host.service.restart",
        "--actor",
        "ops-1",
        "--target-json",
        '{"service":"example.service"}',
        "--idempotency-key",
        "restart:deny:v1",
        "--risk-policy-json",
        '{"host.service.restart":"R2"}',
    )

    _, denied, _ = run_cli(
        capsys,
        *common,
        "action-deny",
        action["action_id"],
        "--approver",
        "operator",
        "--reason",
        "maintenance window closed",
    )
    _, shown, _ = run_cli(capsys, *common, "action-show", action["action_id"])
    _, configured, _ = run_cli(
        capsys,
        *common,
        "budget-set",
        "mission",
        "mission-1",
        "tokens",
        "500",
        "--actor",
        "operator",
    )
    code, budget, error = run_cli(
        capsys, *common, "budget-show", "mission", "mission-1", "tokens"
    )

    assert denied["status"] == "denied"
    assert shown["status"] == "denied"
    assert configured["limit_value"] == 500.0
    assert code == 0
    assert error == ""
    assert budget["used_value"] == 0.0


def test_worker_daemon_does_not_recover_another_executor_inflight_action(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "control.db"
    store = SQLiteStore(database)
    broker = ActionBroker(store, risk_policy={"workspace.write": "R1"})
    broker.grant_capabilities("builder-1", {"workspace.write"}, actor_id="operator")
    action = broker.propose(
        "builder-1",
        "mission-1",
        "workspace.write",
        {"path": "report.md"},
        {},
        idempotency_key="inflight:v1",
    )
    with store.write() as conn:
        conn.execute(
            "UPDATE action_requests SET status = 'executing' WHERE action_id = ?",
            (action.action_id,),
        )
    socket_path = tmp_path / "control.sock"

    code, result, error = run_cli(
        capsys,
        "--db",
        str(database),
        "worker-daemon",
        "--worker-id",
        "worker-1",
        "--role",
        "worker",
        "--allow-command",
        Path(sys.executable).name,
        "--workspace-root",
        str(tmp_path),
        "--capability",
        "context.read",
        "--socket",
        str(socket_path),
        "--max-cycles",
        "1",
        "--poll-seconds",
        "0",
    )

    assert code == 0
    assert error == ""
    assert result["processed"] == 0
    assert broker.get_request(action.action_id).status == "executing"
