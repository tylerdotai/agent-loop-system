from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from agent_loop.model_broker import (
    ModelBroker,
    ModelBrokerAuthorizationError,
    ModelBrokerPolicy,
    ModelBrokerPolicyError,
    ModelBrokerSocketServer,
    broker_call,
)


MODEL = "nemotron-3.5-lightning-30b-a3b"
TOKEN = "one-run-token-that-must-never-be-logged"
PROMPT = "Inspect the repository without changing it."


def authorization() -> dict[str, object]:
    return {
        "worker_id": "audit-worker",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "expires_at": 4_000_000_000.0,
        "model": MODEL,
        "max_tokens": 512,
        "max_prompt_chars": 2_000,
        "temperature": 0.0,
    }


def request() -> dict[str, object]:
    return {
        "request_id": "request-1",
        "control_socket": "/run/agent-loop/audit-worker.sock",
        "run_token": TOKEN,
        "messages": [
            {"role": "system", "content": "Return strict JSON."},
            {"role": "user", "content": PROMPT},
        ],
        "max_tokens": 256,
        "response_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    }


def test_broker_enforces_run_authorization_limits_and_redacted_audit(
    tmp_path: Path,
) -> None:
    authorizations: list[tuple[str, str]] = []
    provider_calls: list[dict[str, object]] = []

    def authorize(control_socket: str, token: str) -> dict[str, object]:
        authorizations.append((control_socket, token))
        return authorization()

    def provider(**payload: object) -> dict[str, object]:
        provider_calls.append(payload)
        return {
            "content": '{"summary":"bounded result"}',
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        }

    audit_log = tmp_path / "audit.jsonl"
    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
            max_tokens=1_024,
            max_prompt_chars=4_000,
            timeout_seconds=30,
            max_concurrent=1,
        ),
        authorizer=authorize,
        provider=provider,
        audit_log=audit_log,
    )

    response = broker.complete(request(), peer_uid=os.getuid())

    assert response["content"] == '{"summary":"bounded result"}'
    assert response["model"] == MODEL
    assert response["identity"] == {
        "worker_id": "audit-worker",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "run_id": "run-1",
    }
    assert len(authorizations) == 2
    assert provider_calls[0]["model"] == MODEL
    assert provider_calls[0]["max_tokens"] == 256
    audit = audit_log.read_text(encoding="utf-8")
    assert TOKEN not in audit
    assert PROMPT not in audit
    event = json.loads(audit)
    assert event["status"] == "completed"
    assert event["worker_id"] == "audit-worker"
    assert event["total_tokens"] == 28


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(max_tokens=513), "token limit"),
        (
            lambda value: value.update(
                messages=[{"role": "user", "content": "x" * 2_001}]
            ),
            "prompt limit",
        ),
    ],
)
def test_broker_rejects_task_policy_limit_violations(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    payload = request()
    mutation(payload)
    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
        ),
        authorizer=lambda _socket, _token: authorization(),
        provider=lambda **_payload: pytest.fail("provider must not be called"),
        audit_log=tmp_path / "audit.jsonl",
    )

    with pytest.raises(ModelBrokerPolicyError, match=message):
        broker.complete(payload, peer_uid=os.getuid())


def test_broker_rejects_wrong_peer_uid_before_token_or_provider_use(
    tmp_path: Path,
) -> None:
    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
        ),
        authorizer=lambda _socket, _token: pytest.fail("authorizer must not be called"),
        provider=lambda **_payload: pytest.fail("provider must not be called"),
        audit_log=tmp_path / "audit.jsonl",
    )

    with pytest.raises(ModelBrokerAuthorizationError, match="peer UID"):
        broker.complete(request(), peer_uid=os.getuid() + 1)


def test_broker_discards_provider_result_when_run_is_cancelled_mid_request(
    tmp_path: Path,
) -> None:
    calls = 0

    def authorize(_socket: str, _token: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("run token is revoked")
        return authorization()

    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
        ),
        authorizer=authorize,
        provider=lambda **_payload: {
            "content": "must be discarded",
            "finish_reason": "stop",
            "usage": {},
        },
        audit_log=tmp_path / "audit.jsonl",
    )

    with pytest.raises(ModelBrokerAuthorizationError, match="no longer active"):
        broker.complete(request(), peer_uid=os.getuid())
    audit = json.loads((tmp_path / "audit.jsonl").read_text())
    assert audit["status"] == "authorization_failed"
    assert "must be discarded" not in json.dumps(audit)


def test_broker_enforces_total_provider_deadline_without_releasing_live_slot(
    tmp_path: Path,
) -> None:
    calls = 0

    def provider(**_payload: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        time.sleep(0.15)
        return {"content": "late", "finish_reason": "stop", "usage": {}}

    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
            timeout_seconds=0.05,
            max_concurrent=1,
        ),
        authorizer=lambda _socket, _token: authorization(),
        provider=provider,
        audit_log=tmp_path / "audit.jsonl",
    )

    started = time.monotonic()
    with pytest.raises(ModelBrokerPolicyError, match="total deadline"):
        broker.complete(request(), peer_uid=os.getuid())
    assert time.monotonic() - started < 0.12
    with pytest.raises(ModelBrokerPolicyError, match="concurrency"):
        broker.complete(request(), peer_uid=os.getuid())
    assert calls == 1


def test_broker_hashes_request_id_and_rejects_tainted_usage_metadata(
    tmp_path: Path,
) -> None:
    payload = request()
    payload["request_id"] = TOKEN
    audit_log = tmp_path / "audit.jsonl"
    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
        ),
        authorizer=lambda _socket, _token: authorization(),
        provider=lambda **_payload: {
            "content": "bounded",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": "PROMPT_LEAK"},
        },
        audit_log=audit_log,
    )

    with pytest.raises(ModelBrokerPolicyError, match="usage prompt_tokens"):
        broker.complete(payload, peer_uid=os.getuid())
    audit = audit_log.read_text(encoding="utf-8")
    assert TOKEN not in audit
    assert "PROMPT_LEAK" not in audit
    assert "request_id_sha256" in audit


def test_real_broker_socket_uses_peer_credentials_and_mode_0660(tmp_path: Path) -> None:
    socket_path = tmp_path / "model.sock"
    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
        ),
        authorizer=lambda _socket, _token: authorization(),
        provider=lambda **_payload: {
            "content": '{"summary":"socket result"}',
            "finish_reason": "stop",
            "usage": {},
        },
        audit_log=tmp_path / "audit.jsonl",
    )

    with ModelBrokerSocketServer(socket_path, broker):
        assert socket_path.stat().st_mode & 0o777 == 0o660
        response = broker_call(socket_path, request(), timeout_seconds=2)

    assert response["content"] == '{"summary":"socket result"}'
    assert not socket_path.exists()


def test_socket_server_bounds_idle_connections_and_recovers(tmp_path: Path) -> None:
    socket_path = tmp_path / "model.sock"
    broker = ModelBroker(
        ModelBrokerPolicy(
            model=MODEL,
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=Path("/run/agent-loop"),
            max_concurrent=1,
            connection_timeout_seconds=0.1,
        ),
        authorizer=lambda _socket, _token: authorization(),
        provider=lambda **_payload: pytest.fail("provider must not be called"),
        audit_log=tmp_path / "audit.jsonl",
    )

    with ModelBrokerSocketServer(socket_path, broker):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
            idle.connect(str(socket_path))
            time.sleep(0.02)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as excess:
                excess.settimeout(0.5)
                excess.connect(str(socket_path))
                try:
                    excess.sendall(b"{}\n")
                except BrokenPipeError:
                    # Busy admission may send its response and close before the
                    # client wins the race to write a request body.
                    pass
                busy = json.loads(excess.recv(4096))
            assert busy["error"]["message"] == "model broker is busy"

            idle.settimeout(0.5)
            timeout_response = json.loads(idle.recv(4096))
            assert timeout_response["error"]["message"] == "request read timed out"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as recovered:
            recovered.settimeout(0.5)
            recovered.connect(str(socket_path))
            recovered.sendall(b"{}\n")
            response = json.loads(recovered.recv(4096))
        assert response["error"]["message"] != "model broker is busy"
