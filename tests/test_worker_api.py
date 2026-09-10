from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

import pytest

from agent_loop.action_broker import ActionBroker
from agent_loop.artifacts import ArtifactStore
from agent_loop.message_board import MessageBoard
from agent_loop.persistence import SQLiteStore
from agent_loop.worker_api import (
    ControlSocketServer,
    RunTokenService,
    TokenAuthorizationError,
    WorkerAPI,
    control_call,
)
from agent_loop.workflow import WorkflowService


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def build_worker_api(tmp_path: Path, capabilities: set[str]):
    clock = FakeClock()
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store, clock=clock)
    mission = workflow.create_mission("Scoped API", "operator", state="active")
    workflow.create_task(mission.mission_id, "Worker task", "worker", actor_id="planner")
    claim = workflow.claim_next("worker-1", {"worker"}, lease_seconds=30)
    assert claim is not None
    tokens = RunTokenService(store, workflow, clock=clock)
    issued = tokens.issue(claim.run.run_id, capabilities, expires_in_seconds=20)
    artifacts = ArtifactStore(store, tmp_path / "artifacts", clock=clock)
    api = WorkerAPI(
        tokens=tokens,
        workflow=workflow,
        board=MessageBoard(store, clock=clock),
        artifacts=artifacts,
    )
    return clock, store, workflow, claim, issued, api


def test_run_token_is_stored_as_hash_and_bound_to_current_run(tmp_path: Path) -> None:
    _, store, _, claim, issued, _ = build_worker_api(tmp_path, {"context.read"})

    with store.read() as conn:
        row = conn.execute("SELECT * FROM run_tokens").fetchone()

    assert row is not None
    assert row["token_hash"] == hashlib.sha256(issued.token.encode()).hexdigest()
    assert issued.token not in " ".join(str(value) for value in row)
    assert row["run_id"] == claim.run.run_id
    assert row["actor_id"] == "worker-1"


def test_worker_message_identity_and_scope_are_derived_from_token(tmp_path: Path) -> None:
    _, _, _, claim, issued, api = build_worker_api(
        tmp_path,
        {"message.publish", "message.read"},
    )

    message = api.post_message(
        issued.token,
        topic=f"mission.{claim.task.mission_id}.general",
        kind="checkpoint",
        body="halfway",
        dedupe_key="checkpoint-1",
    )
    listed = api.list_messages(
        issued.token,
        topic_prefix=f"mission.{claim.task.mission_id}.general",
    )

    assert message.actor_id == "worker-1"
    assert message.mission_id == claim.task.mission_id
    assert message.task_id == claim.task.task_id
    assert listed == [message]


def test_missing_capability_expired_token_and_revoked_run_fail_closed(tmp_path: Path) -> None:
    clock, _, _, claim, issued, api = build_worker_api(tmp_path, {"context.read"})

    with pytest.raises(TokenAuthorizationError, match="capability"):
        api.post_message(
            issued.token,
            topic=f"mission.{claim.task.mission_id}.general",
            kind="question",
            body="not allowed",
        )

    clock.value = 121.0
    with pytest.raises(TokenAuthorizationError, match="expired"):
        api.get_context(issued.token)

    clock.value = 110.0
    api.tokens.revoke_run(claim.run.run_id, actor_id="watchdog")
    with pytest.raises(TokenAuthorizationError, match="revoked"):
        api.get_context(issued.token)


def test_token_becomes_invalid_when_task_run_is_no_longer_current(tmp_path: Path) -> None:
    _, _, workflow, claim, issued, api = build_worker_api(tmp_path, {"context.read"})
    workflow.complete(claim.task.task_id, claim.run.run_id, "worker-1", "finished")

    with pytest.raises(TokenAuthorizationError, match="current active run"):
        api.get_context(issued.token)


def test_token_cannot_use_worker_api_after_task_lease_expires(tmp_path: Path) -> None:
    clock, _, _, claim, _, api = build_worker_api(tmp_path, {"context.read"})
    issued = api.tokens.issue(claim.run.run_id, {"context.read"}, expires_in_seconds=100)
    clock.value = 131.0

    with pytest.raises(TokenAuthorizationError, match="lease expired"):
        api.get_context(issued.token)


def test_run_token_expiry_must_be_finite(tmp_path: Path) -> None:
    _, _, _, claim, _, api = build_worker_api(tmp_path, {"context.read"})

    with pytest.raises(ValueError, match="finite"):
        api.tokens.issue(
            claim.run.run_id,
            {"context.read"},
            expires_in_seconds=float("inf"),
        )


def test_scoped_heartbeat_and_fact_update_use_run_identity(tmp_path: Path) -> None:
    clock, _, _, claim, issued, api = build_worker_api(
        tmp_path,
        {"run.heartbeat", "fact.write", "fact.read"},
    )
    clock.value = 105.0

    task = api.heartbeat(issued.token, lease_seconds=30)
    fact = api.put_fact(
        issued.token,
        fact_key="finding/source-count",
        value={"count": 4},
        expected_version=0,
    )

    assert task.lease_expires_at == 135.0
    assert fact.owner_id == "worker-1"
    assert api.get_fact(issued.token, "finding/source-count") == fact


def test_worker_api_owns_durable_subscription_cursor(tmp_path: Path) -> None:
    _, _, _, claim, issued, api = build_worker_api(
        tmp_path,
        {"subscription.create", "subscription.read", "subscription.ack"},
    )
    prefix = f"mission.{claim.task.mission_id}.research_"
    subscription = api.create_subscription(issued.token, prefix)
    expected = api.board.publish(
        claim.task.mission_id,
        f"{prefix}results",
        "fact_observation",
        "planner",
        "new evidence",
    )

    messages = api.read_subscription(issued.token, subscription.subscription_id)
    acknowledged = api.ack_subscription(
        issued.token,
        subscription.subscription_id,
        expected.message_id,
    )

    assert subscription.subscriber_id == "worker-1"
    assert messages == [expected]
    assert acknowledged.cursor_sequence == expected.sequence
    assert api.read_subscription(issued.token, subscription.subscription_id) == []


def test_worker_api_can_propose_but_cannot_approve_or_execute_action(tmp_path: Path) -> None:
    clock, store, workflow, claim, issued, base_api = build_worker_api(tmp_path, {"action.propose"})
    broker = ActionBroker(store, risk_policy={"host.service.restart": "R2"}, clock=clock)
    broker.grant_capabilities("worker-1", {"host.service.restart"}, actor_id="operator")
    api = WorkerAPI(
        tokens=base_api.tokens,
        workflow=workflow,
        board=base_api.board,
        artifacts=base_api.artifacts,
        action_broker=broker,
    )

    request = api.propose_action(
        issued.token,
        "host.service.restart",
        {"service": "example.service"},
        {"mode": "graceful"},
        idempotency_key="restart:example:v1",
    )

    assert request.actor_id == "worker-1"
    assert request.task_id == claim.task.task_id
    assert request.run_id == claim.run.run_id
    assert request.status == "awaiting_approval"
    assert not hasattr(api, "approve_action")
    assert not hasattr(api, "execute_action")


def test_worker_api_rejects_run_token_in_every_durable_payload(tmp_path: Path) -> None:
    _, store, workflow, claim, issued, base_api = build_worker_api(
        tmp_path,
        {"message.publish", "fact.write", "artifact.write", "action.propose", "subscription.create"},
    )
    broker = ActionBroker(store, risk_policy={"workspace.write": "R1"})
    broker.grant_capabilities("worker-1", {"workspace.write"}, actor_id="operator")
    api = WorkerAPI(
        tokens=base_api.tokens,
        workflow=workflow,
        board=base_api.board,
        artifacts=base_api.artifacts,
        action_broker=broker,
    )

    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.post_message(
            issued.token,
            topic=f"mission.{claim.task.mission_id}.general",
            kind="checkpoint",
            body=f"leak {issued.token}",
        )
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.put_fact(
            issued.token,
            fact_key="leak/token",
            value={"value": issued.token},
            expected_version=0,
        )
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.put_artifact(
            issued.token,
            filename="leak.txt",
            content=issued.token.encode(),
        )
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.propose_action(
            issued.token,
            "workspace.write",
            {"path": "report.md"},
            {"content": issued.token},
            idempotency_key="leak-action:v1",
        )

    encoded = base64.b64encode(issued.token.encode()).decode()
    hexadecimal = issued.token.encode().hex()
    midpoint = len(issued.token) // 2
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.post_message(
            issued.token,
            topic=f"mission.{claim.task.mission_id}.general",
            kind="checkpoint",
            body=encoded,
        )
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.put_fact(
            issued.token,
            fact_key="leak/hex",
            value=hexadecimal,
            expected_version=0,
        )
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.put_artifact(
            issued.token,
            filename="fragment.txt",
            content=issued.token[:midpoint].encode(),
        )
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.propose_action(
            issued.token,
            "workspace.write",
            {"path": "report.md"},
            {"content": issued.token[midpoint:]},
            idempotency_key="leak-fragment-action:v1",
        )
    with pytest.raises(TokenAuthorizationError, match="cannot be persisted"):
        api.create_subscription(
            issued.token,
            topic_prefix=f"mission.{claim.task.mission_id}.{encoded}",
        )

    raw = store.path.read_bytes()
    assert issued.token.encode() not in raw
    assert encoded.encode() not in raw
    assert hexadecimal.encode() not in raw


def test_artifact_upload_is_scoped_to_token_mission_and_task(tmp_path: Path) -> None:
    _, _, _, claim, issued, api = build_worker_api(tmp_path, {"artifact.write", "artifact.read"})

    artifact = api.put_artifact(
        issued.token,
        filename="result.txt",
        content=b"verified",
        media_type="text/plain",
        dedupe_key="result:v1",
    )

    assert artifact.mission_id == claim.task.mission_id
    assert artifact.task_id == claim.task.task_id
    assert artifact.actor_id == "worker-1"
    assert api.read_artifact(issued.token, artifact.artifact_id) == b"verified"


def test_unix_socket_round_trip_exposes_only_typed_worker_methods(tmp_path: Path) -> None:
    _, _, _, claim, issued, api = build_worker_api(
        tmp_path,
        {"message.publish", "message.read", "artifact.write", "artifact.read"},
    )
    socket_path = tmp_path / "control.sock"

    with ControlSocketServer(socket_path, api):
        published = control_call(
            socket_path,
            issued.token,
            "message.publish",
            {
                "topic": f"mission.{claim.task.mission_id}.general",
                "kind": "question",
                "body": "Need review",
            },
        )
        listed = control_call(
            socket_path,
            issued.token,
            "message.list",
            {"topic_prefix": f"mission.{claim.task.mission_id}.general"},
        )
        uploaded = control_call(
            socket_path,
            issued.token,
            "artifact.put",
            {
                "filename": "socket.txt",
                "content_base64": base64.b64encode(b"socket-data").decode(),
            },
        )
        downloaded = control_call(
            socket_path,
            issued.token,
            "artifact.read",
            {"artifact_id": uploaded["artifact_id"]},
        )

    assert published["actor_id"] == "worker-1"
    assert listed[0]["message_id"] == published["message_id"]
    assert base64.b64decode(downloaded["content_base64"]) == b"socket-data"
    assert not socket_path.exists()


def test_unix_socket_supports_artifacts_larger_than_one_megabyte(tmp_path: Path) -> None:
    _, _, _, _, issued, api = build_worker_api(
        tmp_path,
        {"artifact.write", "artifact.read"},
    )
    socket_path = tmp_path / "control.sock"
    content = b"x" * (2 * 1024 * 1024)

    with ControlSocketServer(socket_path, api):
        uploaded = control_call(
            socket_path,
            issued.token,
            "artifact.put",
            {
                "filename": "large.bin",
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        downloaded = control_call(
            socket_path,
            issued.token,
            "artifact.read",
            {"artifact_id": uploaded["artifact_id"]},
        )

    assert base64.b64decode(downloaded["content_base64"]) == content


def test_socket_rejects_unknown_method_without_exposing_traceback(tmp_path: Path) -> None:
    _, _, _, _, issued, api = build_worker_api(tmp_path, {"context.read"})
    socket_path = tmp_path / "control.sock"

    with ControlSocketServer(socket_path, api):
        with pytest.raises(RuntimeError) as error:
            control_call(socket_path, issued.token, "database.execute", {"sql": "SELECT *"})

    message = str(error.value)
    assert "unknown worker API method" in message
    assert "Traceback" not in message
    assert "sqlite3" not in message


def test_run_token_table_never_contains_plaintext_even_after_reopen(tmp_path: Path) -> None:
    clock, store, workflow, claim, issued, _ = build_worker_api(tmp_path, {"context.read"})
    reopened = RunTokenService(SQLiteStore(store.path), workflow, clock=clock)
    assert reopened.authorize(issued.token, "context.read").run_id == claim.run.run_id

    raw = sqlite3.connect(store.path).execute("SELECT token_hash, capabilities_json FROM run_tokens").fetchone()
    assert raw is not None
    assert issued.token not in raw[0]
    assert issued.token not in raw[1]


def test_unix_socket_can_be_owned_by_privilege_separated_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, _, api = build_worker_api(tmp_path, {"context.read"})
    socket_path = tmp_path / "control.sock"
    ownership: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        "agent_loop.worker_api.os.chown",
        lambda path, uid, gid: ownership.append((Path(path), uid, gid)),
    )

    with ControlSocketServer(socket_path, api, owner_uid=23456, owner_gid=23457):
        assert socket_path.stat().st_mode & 0o777 == 0o600

    assert ownership == [(socket_path, 23456, 23457)]
