from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from agent_loop.action_broker import ActionBroker, CapabilityError
from agent_loop.artifacts import ArtifactIntegrityError, ArtifactStore
from agent_loop.message_board import MessageBoard
from agent_loop.persistence import SQLiteStore
from agent_loop.runner_adapter import JsonSubprocessRunner, RunnerPolicyError
from agent_loop.worker_api import ControlSocketServer, RunTokenService, WorkerAPI
from agent_loop.workflow import WorkflowService


def test_topic_prefix_treats_sql_wildcards_as_literal_characters(tmp_path: Path) -> None:
    board = MessageBoard(SQLiteStore(tmp_path / "control.db"))
    subscription = board.subscribe("mission-1", "worker-1", "mission.mission-1.research_")
    expected = board.publish(
        "mission-1",
        "mission.mission-1.research_a",
        "question",
        "planner",
        "expected",
    )
    board.publish(
        "mission-1",
        "mission.mission-1.researchXa",
        "question",
        "planner",
        "must not leak",
    )

    assert board.read_subscription(subscription.subscription_id) == [expected]


def test_action_payload_rejects_plaintext_secret_fields_before_persistence(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    broker = ActionBroker(store, risk_policy={"external.send": "R3"})
    broker.grant_capabilities("sender-1", {"external.send"}, actor_id="operator")

    with pytest.raises(CapabilityError, match="secret field"):
        broker.propose(
            "sender-1",
            "mission-1",
            "external.send",
            {"recipient": "example"},
            {"api_key": "must-not-be-stored"},
            idempotency_key="secret-action:v1",
        )

    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_requests").fetchone()[0] == 0


def test_recovery_marks_orphaned_executing_action_unknown_without_reexecution(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    broker = ActionBroker(store, risk_policy={"artifact.write": "R1"})
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")
    request = broker.propose(
        "builder-1",
        "mission-1",
        "artifact.write",
        {"path": "report.md"},
        {},
        idempotency_key="orphan:v1",
    )
    with store.write() as conn:
        conn.execute("UPDATE action_requests SET status = 'executing' WHERE action_id = ?", (request.action_id,))

    recovered = broker.recover_executing("watchdog")

    assert [receipt.action_id for receipt in recovered] == [request.action_id]
    assert recovered[0].status == "unknown"
    assert "coordinator restarted" in (recovered[0].error or "")
    assert broker.get_request(request.action_id).status == "unknown"


def test_artifact_reader_rejects_symlink_even_when_target_bytes_match_hash(tmp_path: Path) -> None:
    store = ArtifactStore(SQLiteStore(tmp_path / "control.db"), tmp_path / "artifacts")
    artifact = store.put_bytes("mission-1", "task-1", "worker-1", "result.txt", b"same")
    blob = Path(artifact.storage_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"same")
    blob.unlink()
    blob.symlink_to(outside)

    with pytest.raises(ArtifactIntegrityError, match="symbolic link"):
        store.read_bytes(artifact.artifact_id)


def test_json_runner_rejects_cwd_outside_admitted_workspace_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    runner = JsonSubprocessRunner(
        allowed_commands={Path(sys.executable).name},
        allowed_workspace_roots={allowed},
    )
    from agent_loop.runner_adapter import RunRequest

    request = RunRequest(
        "mission-1",
        "task-1",
        "run-1",
        "worker-1",
        "goal",
        {},
        {},
        {},
        str(outside),
    )

    with pytest.raises(RunnerPolicyError, match="outside allowed workspace roots"):
        runner.run(
            request,
            [sys.executable, "-c", "raise SystemExit('must not run')"],
            timeout_seconds=5,
        )


def test_control_socket_permissions_are_owner_only(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("socket mode", "operator", state="active")
    workflow.create_task(mission.mission_id, "task", "worker", actor_id="planner")
    claim = workflow.claim_next("worker-1", {"worker"})
    assert claim is not None
    tokens = RunTokenService(store, workflow)
    api = WorkerAPI(
        tokens=tokens,
        workflow=workflow,
        board=MessageBoard(store),
        artifacts=ArtifactStore(store, tmp_path / "artifacts"),
    )
    socket_path = tmp_path / "control.sock"

    with ControlSocketServer(socket_path, api):
        mode = stat.S_IMODE(os.stat(socket_path).st_mode)

    assert mode == 0o600


def test_database_and_artifact_blob_permissions_are_owner_only(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    sqlite_store = SQLiteStore(database)
    artifacts = ArtifactStore(sqlite_store, tmp_path / "artifacts")
    artifact = artifacts.put_bytes("mission-1", "task-1", "worker-1", "private.txt", b"private")

    assert stat.S_IMODE(os.stat(database).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(artifact.storage_path).st_mode) == 0o600
