from __future__ import annotations

import json
import hashlib
import math
import os
import re
import socket
import socketserver
import stat
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .worker_api import control_call


class ModelBrokerError(RuntimeError):
    """Base error for the local model broker."""


class ModelBrokerAuthorizationError(ModelBrokerError):
    """Raised when the caller or run capability is not authorized."""


class ModelBrokerPolicyError(ModelBrokerError):
    """Raised when a model request exceeds configured policy."""


class ModelBrokerProviderError(ModelBrokerError):
    """Raised when the configured local provider fails its contract."""


@dataclass(frozen=True)
class ModelBrokerPolicy:
    model: str
    endpoint: str
    worker_uid: int
    control_socket_root: Path
    max_tokens: int = 2_048
    max_prompt_chars: int = 24_000
    timeout_seconds: float = 120.0
    connection_timeout_seconds: float = 5.0
    max_concurrent: int = 1
    max_request_bytes: int = 256 * 1024
    max_response_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        endpoint = urllib.parse.urlsplit(self.endpoint)
        if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("endpoint must be an HTTP loopback URL")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("endpoint must not contain credentials, query, or fragment")
        if isinstance(self.worker_uid, bool) or not isinstance(self.worker_uid, int) or self.worker_uid < 0:
            raise ValueError("worker_uid must be a non-negative integer")
        root = Path(self.control_socket_root).expanduser().resolve()
        if not root.is_absolute():
            raise ValueError("control_socket_root must be absolute")
        object.__setattr__(self, "control_socket_root", root)
        for name in (
            "max_tokens",
            "max_prompt_chars",
            "max_concurrent",
            "max_request_bytes",
            "max_response_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("timeout_seconds", "connection_timeout_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")


Authorizer = Callable[[str, str], dict[str, Any]]
Provider = Callable[..., dict[str, Any]]
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ModelBroker:
    """Authorize one-run model requests and relay them to one loopback model."""

    def __init__(
        self,
        policy: ModelBrokerPolicy,
        *,
        audit_log: str | Path,
        authorizer: Authorizer | None = None,
        provider: Provider | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self.audit_log = Path(audit_log).expanduser().resolve()
        self.authorizer = authorizer or self._authorize_with_control_plane
        self.provider = provider or self._openai_provider
        self.clock = clock
        self._slots = threading.BoundedSemaphore(policy.max_concurrent)
        self._audit_lock = threading.Lock()

    def complete(self, payload: Mapping[str, Any], *, peer_uid: int) -> dict[str, Any]:
        started_at = self.clock()
        request_id = self._request_id(payload.get("request_id"))
        if peer_uid != self.policy.worker_uid:
            self._audit(request_id, "peer_denied", started_at, peer_uid=peer_uid)
            raise ModelBrokerAuthorizationError("peer UID is not authorized")
        control_socket = self._control_socket(payload.get("control_socket"))
        run_token = self._text(payload.get("run_token"), "run_token", maximum=512)
        try:
            authorization = self.authorizer(str(control_socket), run_token)
        except Exception as exc:
            self._audit(request_id, "authorization_failed", started_at, error_type=type(exc).__name__)
            raise ModelBrokerAuthorizationError("run authorization failed") from exc
        identity, limits = self._authorization(authorization)
        messages = self._messages(payload.get("messages"))
        prompt_chars = sum(len(message["content"]) for message in messages)
        max_tokens = self._positive_integer(payload.get("max_tokens"), "max_tokens")
        task_max_tokens = limits["max_tokens"]
        task_max_prompt_chars = limits["max_prompt_chars"]
        if max_tokens > min(self.policy.max_tokens, task_max_tokens):
            raise ModelBrokerPolicyError("request exceeds token limit")
        if prompt_chars > min(self.policy.max_prompt_chars, task_max_prompt_chars):
            raise ModelBrokerPolicyError("request exceeds prompt limit")
        response_schema = self._response_schema(payload.get("response_schema"))
        if not self._slots.acquire(blocking=False):
            raise ModelBrokerPolicyError("model concurrency limit reached")
        provider_timeout = min(
            self.policy.timeout_seconds,
            limits["expires_at"] - self.clock(),
        )
        if provider_timeout <= 0:
            self._slots.release()
            raise ModelBrokerAuthorizationError("run authorization expired before model request")
        finished = threading.Event()
        provider_outcome: dict[str, Any] = {}

        def invoke_provider() -> None:
            try:
                provider_outcome["result"] = self.provider(
                    endpoint=self.policy.endpoint,
                    model=self.policy.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=limits["temperature"],
                    response_schema=response_schema,
                    timeout_seconds=provider_timeout,
                    max_response_bytes=self.policy.max_response_bytes,
                )
            except BaseException as exc:
                provider_outcome["error"] = exc
            finally:
                self._slots.release()
                finished.set()

        provider_thread = threading.Thread(
            target=invoke_provider,
            name="agent-loop-model-provider",
            daemon=True,
        )
        provider_thread.start()
        if not finished.wait(provider_timeout):
            self._audit(
                request_id,
                "provider_failed",
                started_at,
                identity=identity,
                prompt_chars=prompt_chars,
                error_type="ProviderDeadlineExceeded",
            )
            raise ModelBrokerPolicyError("model provider exceeded total deadline")
        provider_error = provider_outcome.get("error")
        if provider_error is not None:
            self._audit(
                request_id,
                "provider_failed",
                started_at,
                identity=identity,
                prompt_chars=prompt_chars,
                error_type=type(provider_error).__name__,
            )
            if isinstance(provider_error, ModelBrokerError):
                raise provider_error
            raise ModelBrokerProviderError("local model request failed") from provider_error
        provider_result = provider_outcome.get("result")
        try:
            current = self.authorizer(str(control_socket), run_token)
            current_identity, _ = self._authorization(current)
            if current_identity != identity:
                raise ModelBrokerAuthorizationError("run identity changed during model request")
        except Exception as exc:
            self._audit(
                request_id,
                "authorization_failed",
                started_at,
                identity=identity,
                prompt_chars=prompt_chars,
                error_type=type(exc).__name__,
            )
            raise ModelBrokerAuthorizationError("run is no longer active; provider result discarded") from exc
        try:
            result = self._provider_result(provider_result)
        except Exception as exc:
            self._audit(
                request_id,
                "provider_failed",
                started_at,
                identity=identity,
                prompt_chars=prompt_chars,
                error_type=type(exc).__name__,
            )
            raise
        usage = result["usage"]
        self._audit(
            request_id,
            "completed",
            started_at,
            identity=identity,
            prompt_chars=prompt_chars,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        return {
            "request_id": request_id,
            "model": self.policy.model,
            "identity": identity,
            **result,
        }

    def _authorize_with_control_plane(self, control_socket: str, token: str) -> dict[str, Any]:
        path = Path(control_socket)
        try:
            metadata = path.stat()
        except OSError as exc:
            raise ModelBrokerAuthorizationError("control socket is unavailable") from exc
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ModelBrokerAuthorizationError("control path is not a socket")
        if metadata.st_uid != self.policy.worker_uid or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o660}:
            raise ModelBrokerAuthorizationError("control socket ownership or mode is invalid")
        result = control_call(path, token, "model.authorize", timeout_seconds=5)
        if not isinstance(result, dict):
            raise ModelBrokerAuthorizationError("control plane returned invalid authorization")
        return result

    def _authorization(
        self,
        value: Mapping[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        if not isinstance(value, Mapping):
            raise ModelBrokerAuthorizationError("authorization must be an object")
        identity = {
            field: self._text(value.get(field), field, maximum=256)
            for field in ("worker_id", "mission_id", "task_id", "run_id")
        }
        model = self._text(value.get("model"), "model", maximum=256)
        if model != self.policy.model:
            raise ModelBrokerAuthorizationError("task model is not served by this broker")
        expires_at = value.get("expires_at")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= self.clock()
        ):
            raise ModelBrokerAuthorizationError("run authorization is expired")
        temperature = value.get("temperature")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
            or not 0 <= float(temperature) <= 2
        ):
            raise ModelBrokerAuthorizationError("authorized temperature is invalid")
        limits = {
            "expires_at": float(expires_at),
            "max_tokens": self._positive_integer(value.get("max_tokens"), "authorized max_tokens"),
            "max_prompt_chars": self._positive_integer(
                value.get("max_prompt_chars"), "authorized max_prompt_chars"
            ),
            "temperature": float(temperature),
        }
        return identity, limits

    def _control_socket(self, value: Any) -> Path:
        path = Path(self._text(value, "control_socket", maximum=4_096)).expanduser().resolve()
        if path == self.policy.control_socket_root or not path.is_relative_to(
            self.policy.control_socket_root
        ):
            raise ModelBrokerAuthorizationError("control socket is outside the trusted root")
        return path

    def _messages(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or not 1 <= len(value) <= 32:
            raise ModelBrokerPolicyError("messages must contain between 1 and 32 entries")
        messages: list[dict[str, str]] = []
        system_count = 0
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) != {"role", "content"}:
                raise ModelBrokerPolicyError("each message must contain only role and content")
            role = item.get("role")
            if role not in {"system", "user", "assistant"}:
                raise ModelBrokerPolicyError("message role is not allowed")
            if role == "system":
                system_count += 1
                if index != 0 or system_count > 1:
                    raise ModelBrokerPolicyError("the single system message must be first")
            content = self._text(item.get("content"), "message content", maximum=self.policy.max_prompt_chars)
            messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _response_schema(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("type") != "object":
            raise ModelBrokerPolicyError("response_schema must be an object JSON schema")
        encoded = json.dumps(value, separators=(",", ":"))
        if len(encoded) > 24_000:
            raise ModelBrokerPolicyError("response_schema exceeds maximum size")
        return value

    @staticmethod
    def _provider_result(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ModelBrokerProviderError("provider result must be an object")
        content = value.get("content")
        finish_reason = value.get("finish_reason")
        usage = value.get("usage", {})
        if not isinstance(content, str) or not content.strip():
            raise ModelBrokerProviderError("provider returned no final content")
        if finish_reason != "stop":
            raise ModelBrokerProviderError(f"provider did not finish cleanly: {finish_reason}")
        if not isinstance(usage, dict):
            raise ModelBrokerProviderError("provider usage must be an object")
        normalized_usage: dict[str, int] = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            count = usage.get(field)
            if count is None:
                continue
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ModelBrokerPolicyError(f"provider usage {field} must be a non-negative integer")
            normalized_usage[field] = count
        return {"content": content, "finish_reason": finish_reason, "usage": normalized_usage}

    @staticmethod
    def _openai_provider(
        *,
        endpoint: str,
        model: str,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        temperature: float,
        response_schema: dict[str, Any] | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_loop_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        request = urllib.request.Request(
            urllib.parse.urljoin(endpoint.rstrip("/") + "/", "v1/chat/completions"),
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise ModelBrokerProviderError(f"local model returned HTTP {exc.code}") from exc
        except (OSError, TimeoutError) as exc:
            raise ModelBrokerProviderError("local model request did not complete") from exc
        if len(raw) > max_response_bytes:
            raise ModelBrokerProviderError("local model response exceeds maximum size")
        try:
            payload = json.loads(raw)
            choice = payload["choices"][0]
            message = choice["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelBrokerProviderError("local model returned an invalid response") from exc
        return {
            "content": message.get("content"),
            "finish_reason": choice.get("finish_reason"),
            "usage": payload.get("usage", {}),
        }

    def _audit(
        self,
        request_id: str,
        status_value: str,
        started_at: float,
        *,
        identity: Mapping[str, str] | None = None,
        **metadata: Any,
    ) -> None:
        event: dict[str, Any] = {
            "request_id_sha256": hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
            "status": status_value,
            "created_at": self.clock(),
            "elapsed_ms": max(0, round((self.clock() - started_at) * 1_000)),
        }
        if identity is not None:
            event.update(identity)
        event.update(metadata)
        self.audit_log.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        encoded = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
        with self._audit_lock:
            with self.audit_log.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.audit_log, 0o600)

    @staticmethod
    def _request_id(value: Any) -> str:
        if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
            raise ModelBrokerPolicyError("request_id is invalid")
        return value

    @staticmethod
    def _text(value: Any, name: str, *, maximum: int) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ModelBrokerPolicyError(f"{name} must be a non-empty bounded string")
        return value

    @staticmethod
    def _positive_integer(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ModelBrokerPolicyError(f"{name} must be a positive integer")
        return value


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, *args: Any, max_handlers: int, **kwargs: Any) -> None:
        self._handler_slots = threading.BoundedSemaphore(max_handlers)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._handler_slots.acquire(blocking=False):
            encoded = json.dumps(
                _error("model broker is busy", "ModelBrokerPolicyError"),
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            try:
                request.settimeout(0.1)
                request.sendall(encoded)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


class ModelBrokerSocketServer:
    """Line-delimited broker protocol with kernel-provided peer identity."""

    def __init__(self, path: str | Path, broker: ModelBroker) -> None:
        self.path = Path(path).expanduser().resolve()
        self.broker = broker
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ModelBrokerSocketServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("model broker socket is already running")
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if self.path.exists():
            if not self.path.is_socket():
                raise RuntimeError(f"refusing to replace non-socket path: {self.path}")
            self.path.unlink()
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                peer_uid = _peer_uid(self.request)
                self.request.settimeout(outer.broker.policy.connection_timeout_seconds)
                try:
                    raw = self.rfile.readline(outer.broker.policy.max_request_bytes + 1)
                    if len(raw) > outer.broker.policy.max_request_bytes:
                        response = _error("request exceeds maximum size", "ModelBrokerPolicyError")
                    else:
                        response = outer._handle(raw, peer_uid)
                except TimeoutError:
                    response = _error("request read timed out", "ModelBrokerPolicyError")
                encoded = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
                try:
                    self.wfile.write(encoded[: outer.broker.policy.max_response_bytes])
                except OSError:
                    pass

        self._server = _ThreadingUnixServer(
            str(self.path),
            Handler,
            max_handlers=self.broker.policy.max_concurrent,
        )
        os.chmod(self.path, 0o660)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="agent-loop-model-broker",
            daemon=True,
        )
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

    def _handle(self, raw: bytes, peer_uid: int) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ModelBrokerPolicyError("request must be a JSON object")
            return {"ok": True, "result": self.broker.complete(payload, peer_uid=peer_uid)}
        except Exception as exc:
            return _error(str(exc), type(exc).__name__)


def _peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise ModelBrokerAuthorizationError("Unix peer credentials are unavailable")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", raw)
    return uid


def _error(message: str, error_type: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}


def broker_call(
    socket_path: str | Path,
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float = 120,
    max_response_bytes: int = 512 * 1024,
) -> dict[str, Any]:
    encoded = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout_seconds)
        client.connect(str(Path(socket_path).expanduser().resolve()))
        client.sendall(encoded)
        received = bytearray()
        while not received.endswith(b"\n"):
            chunk = client.recv(65_536)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > max_response_bytes:
                raise ModelBrokerProviderError("broker response exceeds maximum size")
    try:
        response = json.loads(received)
    except json.JSONDecodeError as exc:
        raise ModelBrokerProviderError("broker returned invalid JSON") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        error = response.get("error", {}) if isinstance(response, dict) else {}
        raise ModelBrokerError(
            f"{error.get('type', 'ModelBrokerError')}: {error.get('message', 'request failed')}"
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise ModelBrokerProviderError("broker returned an invalid result")
    return result
