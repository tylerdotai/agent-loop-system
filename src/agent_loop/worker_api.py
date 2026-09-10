from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import socket
import socketserver
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .action_broker import ActionBroker, ActionRequest
from .artifacts import Artifact, ArtifactStore
from .message_board import Fact, Message, MessageBoard, Subscription
from .persistence import SQLiteStore
from .workflow import Task, WorkflowService


class TokenAuthorizationError(PermissionError):
    """Raised when a run token is invalid, expired, revoked, or out of scope."""


@dataclass(frozen=True)
class RunPrincipal:
    actor_id: str
    mission_id: str
    task_id: str
    run_id: str
    capabilities: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class IssuedRunToken:
    token: str
    principal: RunPrincipal


_RUN_TOKEN_SCHEMA = """
INSERT OR IGNORE INTO schema_meta(component, version) VALUES ('worker_api', 1);

CREATE TABLE IF NOT EXISTS run_tokens (
    token_hash TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_tokens_run ON run_tokens(run_id);
"""


class RunTokenService:
    """Issue plaintext-once tokens and persist only their SHA-256 hashes."""

    def __init__(
        self,
        store: SQLiteStore,
        workflow: WorkflowService,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.workflow = workflow
        self.clock = clock
        with self.store.connect() as conn:
            conn.executescript(_RUN_TOKEN_SCHEMA)

    def issue(
        self,
        run_id: str,
        capabilities: Iterable[str],
        *,
        expires_in_seconds: float,
    ) -> IssuedRunToken:
        normalized = self._capabilities(capabilities)
        if (
            isinstance(expires_in_seconds, bool)
            or not isinstance(expires_in_seconds, (int, float))
            or not math.isfinite(float(expires_in_seconds))
            or expires_in_seconds <= 0
        ):
            raise ValueError("expires_in_seconds must be positive and finite")
        timestamp = self.clock()
        expires_at = timestamp + float(expires_in_seconds)
        token = secrets.token_urlsafe(32)
        token_hash = self._hash(token)
        with self.store.write() as conn:
            row = conn.execute(
                """
                SELECT r.worker_id, r.mission_id, r.task_id, r.status,
                       t.status AS task_status, t.current_run_id
                FROM task_runs r
                JOIN tasks t ON t.task_id = r.task_id
                WHERE r.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["task_status"] != "running"
                or row["current_run_id"] != run_id
            ):
                raise TokenAuthorizationError("token can be issued only for a current active run")
            conn.execute(
                """
                INSERT INTO run_tokens(
                    token_hash, actor_id, mission_id, task_id, run_id,
                    capabilities_json, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    row["worker_id"],
                    row["mission_id"],
                    row["task_id"],
                    run_id,
                    json.dumps(normalized, separators=(",", ":")),
                    expires_at,
                    timestamp,
                ),
            )
            self.store.append_event(
                conn,
                kind="run_token.issued",
                actor_id="token-service",
                actor_type="system",
                mission_id=str(row["mission_id"]),
                task_id=str(row["task_id"]),
                run_id=run_id,
                payload={"capabilities": list(normalized), "expires_at": expires_at},
                created_at=timestamp,
            )
            principal = RunPrincipal(
                actor_id=str(row["worker_id"]),
                mission_id=str(row["mission_id"]),
                task_id=str(row["task_id"]),
                run_id=run_id,
                capabilities=normalized,
                expires_at=expires_at,
            )
            return IssuedRunToken(token=token, principal=principal)

    def authorize(self, token: str, capability: str) -> RunPrincipal:
        token_hash = self._hash(self._require_text(token, "token"))
        capability = self._require_text(capability, "capability")
        timestamp = self.clock()
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM run_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if row is None:
                raise TokenAuthorizationError("unknown run token")
            if row["revoked_at"] is not None:
                raise TokenAuthorizationError("run token is revoked")
            if float(row["expires_at"]) <= timestamp:
                raise TokenAuthorizationError("run token is expired")
            capabilities = tuple(json.loads(row["capabilities_json"]))
            if capability not in capabilities:
                raise TokenAuthorizationError(f"run token lacks capability: {capability}")
            run = conn.execute(
                """
                SELECT r.status, r.worker_id, t.status AS task_status,
                       t.current_run_id, t.lease_expires_at AS task_lease_expires_at
                FROM task_runs r JOIN tasks t ON t.task_id = r.task_id
                WHERE r.run_id = ? AND r.task_id = ? AND r.mission_id = ?
                """,
                (row["run_id"], row["task_id"], row["mission_id"]),
            ).fetchone()
            if (
                run is None
                or run["status"] != "running"
                or run["task_status"] != "running"
                or run["current_run_id"] != row["run_id"]
                or run["worker_id"] != row["actor_id"]
            ):
                raise TokenAuthorizationError("token is not associated with a current active run")
            if (
                run["task_lease_expires_at"] is None
                or float(run["task_lease_expires_at"]) <= timestamp
            ):
                raise TokenAuthorizationError("task lease expired")
            return RunPrincipal(
                actor_id=str(row["actor_id"]),
                mission_id=str(row["mission_id"]),
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                capabilities=capabilities,
                expires_at=float(row["expires_at"]),
            )

    def revoke_run(self, run_id: str, *, actor_id: str) -> int:
        timestamp = self.clock()
        with self.store.write() as conn:
            cursor = conn.execute(
                "UPDATE run_tokens SET revoked_at = ? WHERE run_id = ? AND revoked_at IS NULL",
                (timestamp, run_id),
            )
            count = cursor.rowcount
            if count:
                self.store.append_event(
                    conn,
                    kind="run_token.revoked",
                    actor_id=actor_id,
                    actor_type="system",
                    run_id=run_id,
                    payload={"count": count},
                    created_at=timestamp,
                )
            return count

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value:
            raise TokenAuthorizationError(f"{field_name} must not be empty")
        return value

    @staticmethod
    def _capabilities(values: Iterable[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
            raise ValueError("capabilities must be a sequence of strings")
        normalized = tuple(sorted(set(values)))
        if not normalized or not all(isinstance(value, str) and value for value in normalized):
            raise ValueError("capabilities must contain non-empty strings")
        return normalized


class WorkerAPI:
    """Capability-scoped worker surface; identity comes only from the run token."""

    def __init__(
        self,
        *,
        tokens: RunTokenService,
        workflow: WorkflowService,
        board: MessageBoard,
        artifacts: ArtifactStore,
        action_broker: ActionBroker | None = None,
    ) -> None:
        self.tokens = tokens
        self.workflow = workflow
        self.board = board
        self.artifacts = artifacts
        self.action_broker = action_broker

    def get_context(self, token: str) -> dict[str, Any]:
        principal = self.tokens.authorize(token, "context.read")
        return self.workflow.build_task_context(principal.task_id)

    def post_message(
        self,
        token: str,
        *,
        topic: str,
        kind: str,
        body: str,
        recipients: Sequence[str] = (),
        correlation_id: str | None = None,
        reply_to: str | None = None,
        subject: str = "",
        data: dict[str, Any] | None = None,
        artifact_refs: Sequence[str] = (),
        dedupe_key: str | None = None,
        expires_at: float | None = None,
    ) -> Message:
        principal = self.tokens.authorize(token, "message.publish")
        self._reject_token_persistence(
            token,
            {
                "topic": topic,
                "kind": kind,
                "body": body,
                "recipients": recipients,
                "correlation_id": correlation_id,
                "reply_to": reply_to,
                "subject": subject,
                "data": data,
                "artifact_refs": artifact_refs,
                "dedupe_key": dedupe_key,
            },
        )
        return self.board.publish(
            principal.mission_id,
            topic,
            kind,
            principal.actor_id,
            body,
            task_id=principal.task_id,
            recipients=recipients,
            correlation_id=correlation_id,
            reply_to=reply_to,
            subject=subject,
            data=data,
            artifact_refs=artifact_refs,
            dedupe_key=dedupe_key,
            expires_at=expires_at,
        )

    def list_messages(self, token: str, *, topic_prefix: str | None = None, limit: int = 100) -> list[Message]:
        principal = self.tokens.authorize(token, "message.read")
        return self.board.list_messages(principal.mission_id, topic_prefix=topic_prefix, limit=limit)

    def create_subscription(self, token: str, topic_prefix: str) -> Subscription:
        principal = self.tokens.authorize(token, "subscription.create")
        self._reject_token_persistence(token, topic_prefix)
        return self.board.subscribe(principal.mission_id, principal.actor_id, topic_prefix)

    def read_subscription(
        self,
        token: str,
        subscription_id: str,
        *,
        limit: int = 100,
    ) -> list[Message]:
        principal = self.tokens.authorize(token, "subscription.read")
        subscription = self._owned_subscription(principal, subscription_id)
        return self.board.read_subscription(subscription.subscription_id, limit=limit)

    def ack_subscription(
        self,
        token: str,
        subscription_id: str,
        message_id: str,
    ) -> Subscription:
        principal = self.tokens.authorize(token, "subscription.ack")
        subscription = self._owned_subscription(principal, subscription_id)
        return self.board.ack(subscription.subscription_id, message_id)

    def put_fact(
        self,
        token: str,
        *,
        fact_key: str,
        value: Any,
        expected_version: int,
    ) -> Fact:
        principal = self.tokens.authorize(token, "fact.write")
        self._reject_token_persistence(token, {"fact_key": fact_key, "value": value})
        return self.board.put_fact(
            principal.mission_id,
            fact_key,
            value,
            principal.actor_id,
            expected_version=expected_version,
        )

    def get_fact(self, token: str, fact_key: str) -> Fact | None:
        principal = self.tokens.authorize(token, "fact.read")
        return self.board.get_fact(principal.mission_id, fact_key)

    def heartbeat(self, token: str, *, lease_seconds: float) -> Task:
        principal = self.tokens.authorize(token, "run.heartbeat")
        return self.workflow.heartbeat(
            principal.task_id,
            principal.run_id,
            principal.actor_id,
            lease_seconds=lease_seconds,
        )

    def put_artifact(
        self,
        token: str,
        *,
        filename: str,
        content: bytes,
        media_type: str | None = None,
        dedupe_key: str | None = None,
    ) -> Artifact:
        principal = self.tokens.authorize(token, "artifact.write")
        self._reject_token_persistence(
            token,
            {
                "filename": filename,
                "content": content,
                "media_type": media_type,
                "dedupe_key": dedupe_key,
            },
        )
        return self.artifacts.put_bytes(
            principal.mission_id,
            principal.task_id,
            principal.actor_id,
            filename,
            content,
            media_type=media_type,
            dedupe_key=dedupe_key,
        )

    def read_artifact(self, token: str, artifact_id: str) -> bytes:
        principal = self.tokens.authorize(token, "artifact.read")
        artifact = self.artifacts.get(artifact_id)
        if artifact.mission_id != principal.mission_id:
            raise TokenAuthorizationError("artifact belongs to another mission")
        return self.artifacts.read_bytes(artifact_id)

    def propose_action(
        self,
        token: str,
        action_type: str,
        target: dict[str, Any],
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ActionRequest:
        principal = self.tokens.authorize(token, "action.propose")
        if self.action_broker is None:
            raise TokenAuthorizationError("action proposals are not enabled for this worker API")
        self._reject_token_persistence(
            token,
            {
                "action_type": action_type,
                "target": target,
                "arguments": arguments,
                "idempotency_key": idempotency_key,
            },
        )
        return self.action_broker.propose(
            principal.actor_id,
            principal.mission_id,
            action_type,
            target,
            arguments,
            task_id=principal.task_id,
            run_id=principal.run_id,
            idempotency_key=idempotency_key,
        )

    def _owned_subscription(
        self,
        principal: RunPrincipal,
        subscription_id: str,
    ) -> Subscription:
        subscription = self.board.get_subscription(subscription_id)
        if (
            subscription.mission_id != principal.mission_id
            or subscription.subscriber_id != principal.actor_id
        ):
            raise TokenAuthorizationError("subscription belongs to another worker or mission")
        return subscription

    @staticmethod
    def _reject_token_persistence(token: str, value: Any) -> None:
        token_bytes = token.encode("utf-8")
        variants = {
            token,
            token_bytes.hex(),
            base64.b64encode(token_bytes).decode("ascii"),
            base64.urlsafe_b64encode(token_bytes).decode("ascii"),
            base64.b32encode(token_bytes).decode("ascii"),
        }
        variants.update(item.rstrip("=") for item in tuple(variants) if item != token)
        minimum_fragment = max(12, min(24, len(token) // 3))
        variants.update(
            token[index : index + minimum_fragment]
            for index in range(len(token) - minimum_fragment + 1)
        )
        encoded_variants = tuple(item.encode("utf-8") for item in variants)
        seen: set[int] = set()

        def visit(current: Any) -> None:
            if isinstance(current, str):
                if any(secret and secret in current for secret in variants):
                    raise TokenAuthorizationError("run token cannot be persisted")
                return
            if isinstance(current, bytes):
                if any(secret and secret in current for secret in encoded_variants):
                    raise TokenAuthorizationError("run token cannot be persisted")
                return
            if isinstance(current, Mapping):
                identity = id(current)
                if identity in seen:
                    return
                seen.add(identity)
                for key, nested in current.items():
                    visit(key)
                    visit(nested)
                return
            if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
                identity = id(current)
                if identity in seen:
                    return
                seen.add(identity)
                for nested in current:
                    visit(nested)

        visit(value)


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class ControlSocketServer:
    """Line-delimited JSON worker API over a mode-0600 Unix socket."""

    max_request_bytes = 36 * 1024 * 1024
    max_response_bytes = 36 * 1024 * 1024

    def __init__(
        self,
        path: str | Path,
        api: WorkerAPI,
        *,
        owner_uid: int | None = None,
        owner_gid: int | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.api = api
        if (owner_uid is None) != (owner_gid is None):
            raise ValueError("socket owner_uid and owner_gid must be configured together")
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ControlSocketServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("control socket server is already running")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if not self.path.is_socket():
                raise RuntimeError(f"refusing to replace non-socket path: {self.path}")
            self.path.unlink()
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                raw = self.rfile.readline(outer.max_request_bytes + 1)
                if len(raw) > outer.max_request_bytes:
                    response = outer._error("request exceeds maximum size")
                else:
                    response = outer._handle(raw)
                self.wfile.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")

        self._server = _ThreadingUnixServer(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        if self.owner_uid is not None and self.owner_gid is not None:
            os.chown(self.path, self.owner_uid, self.owner_gid)
        self._thread = threading.Thread(target=self._server.serve_forever, name="agent-loop-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if self.path.exists() and self.path.is_socket():
            self.path.unlink()

    def _handle(self, raw: bytes) -> dict[str, Any]:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            token = request.get("token")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(token, str) or not token:
                raise ValueError("token must be a non-empty string")
            if not isinstance(method, str) or not method:
                raise ValueError("method must be a non-empty string")
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            result = self._dispatch(token, method, params)
            return {"ok": True, "result": result}
        except Exception as exc:
            return self._error(str(exc), type(exc).__name__)

    def _dispatch(self, token: str, method: str, params: dict[str, Any]) -> Any:
        if method == "context.get":
            return self.api.get_context(token)
        if method == "message.publish":
            return asdict(self.api.post_message(token, **params))
        if method == "message.list":
            return [asdict(message) for message in self.api.list_messages(token, **params)]
        if method == "subscription.create":
            return asdict(self.api.create_subscription(token, **params))
        if method == "subscription.read":
            return [asdict(message) for message in self.api.read_subscription(token, **params)]
        if method == "subscription.ack":
            return asdict(self.api.ack_subscription(token, **params))
        if method == "fact.put":
            return asdict(self.api.put_fact(token, **params))
        if method == "fact.get":
            fact = self.api.get_fact(token, **params)
            return None if fact is None else asdict(fact)
        if method == "heartbeat":
            return asdict(self.api.heartbeat(token, **params))
        if method == "artifact.put":
            encoded = params.pop("content_base64", None)
            if not isinstance(encoded, str):
                raise ValueError("content_base64 must be a string")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("content_base64 is invalid") from exc
            return asdict(self.api.put_artifact(token, content=content, **params))
        if method == "artifact.read":
            content = self.api.read_artifact(token, **params)
            return {"content_base64": base64.b64encode(content).decode("ascii")}
        if method == "action.propose":
            return asdict(self.api.propose_action(token, **params))
        raise ValueError(f"unknown worker API method: {method}")

    @staticmethod
    def _error(message: str, error_type: str = "RequestError") -> dict[str, Any]:
        return {"ok": False, "error": {"type": error_type, "message": message}}


def control_call(
    socket_path: str | Path,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 5,
) -> Any:
    request = json.dumps(
        {"token": token, "method": method, "params": params or {}},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout_seconds)
        client.connect(str(Path(socket_path).expanduser().resolve()))
        client.sendall(request)
        received = bytearray()
        while not received.endswith(b"\n"):
            chunk = client.recv(65_536)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > ControlSocketServer.max_response_bytes:
                raise RuntimeError("control socket response exceeds maximum size")
    try:
        response = json.loads(received)
    except json.JSONDecodeError as exc:
        raise RuntimeError("control socket returned invalid JSON") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        error = response.get("error", {}) if isinstance(response, dict) else {}
        raise RuntimeError(f"{error.get('type', 'RequestError')}: {error.get('message', 'request failed')}")
    return response.get("result")
