from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .command_runner import Redactor
from .persistence import SQLiteStore


RISK_CLASSES = frozenset({"R0", "R1", "R2", "R3", "R4"})
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "secret",
        "token",
    }
)


class CapabilityError(PermissionError):
    """Raised when an actor lacks a required capability or live run."""


class ApprovalError(PermissionError):
    """Raised when approval is absent, stale, or not valid for an action."""


class IdempotencyConflictError(RuntimeError):
    """Raised when one idempotency key is reused for a different payload."""


class ActionOutcomeUnknown(RuntimeError):
    """Raised after an effect may have occurred but readback cannot prove it."""


class BudgetExceededError(RuntimeError):
    """Raised when an atomic reservation would exceed a configured limit."""


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    actor_id: str
    mission_id: str
    task_id: str | None
    run_id: str | None
    action_type: str
    target: dict[str, Any]
    arguments: dict[str, Any]
    risk_class: str
    status: str
    payload_hash: str
    idempotency_key: str
    approved_by: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class ActionReceipt:
    receipt_id: str
    action_id: str
    status: str
    result: dict[str, Any]
    verification: dict[str, Any]
    error: str | None
    created_at: float


@dataclass(frozen=True)
class Budget:
    scope_type: str
    scope_id: str
    unit: str
    limit_value: float
    used_value: float
    reserved_value: float
    updated_at: float


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    scope_type: str
    scope_id: str
    unit: str
    amount: float
    status: str
    created_at: float
    updated_at: float


class ActionHandler(Protocol):
    def execute(self, request: ActionRequest) -> dict[str, Any]: ...

    def verify(self, request: ActionRequest, result: dict[str, Any]) -> dict[str, Any]: ...


_ACTION_SCHEMA = """
INSERT OR IGNORE INTO schema_meta(component, version) VALUES ('action_broker', 1);

CREATE TABLE IF NOT EXISTS capability_grants (
    agent_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(agent_id, capability)
);

CREATE TABLE IF NOT EXISTS action_requests (
    action_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT,
    action_type TEXT NOT NULL,
    target_json TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    approved_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(mission_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS approvals (
    action_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    reason TEXT,
    decided_at REAL NOT NULL,
    FOREIGN KEY(action_id) REFERENCES action_requests(action_id)
);

CREATE TABLE IF NOT EXISTS action_receipts (
    receipt_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(action_id) REFERENCES action_requests(action_id)
);

CREATE TABLE IF NOT EXISTS budgets (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    limit_value REAL NOT NULL,
    used_value REAL NOT NULL DEFAULT 0,
    reserved_value REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY(scope_type, scope_id, unit)
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(scope_type, scope_id, unit)
        REFERENCES budgets(scope_type, scope_id, unit)
);
"""


class ActionBroker:
    """Authorize, approve, execute, and verify typed side effects."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        risk_policy: Mapping[str, str],
        clock: Callable[[], float] = time.time,
        redact_values: Sequence[str] | set[str] = (),
        redact_patterns: Sequence[str] = (),
    ) -> None:
        self.store = store
        self.clock = clock
        self.risk_policy = dict(risk_policy)
        self.redactor = Redactor(values=tuple(redact_values), patterns=tuple(redact_patterns))
        invalid = {risk for risk in self.risk_policy.values() if risk not in RISK_CLASSES}
        if invalid:
            raise ValueError(f"unknown risk classes: {sorted(invalid)}")
        with self.store.connect() as conn:
            conn.executescript(_ACTION_SCHEMA)

    def grant_capabilities(self, agent_id: str, capabilities: Sequence[str] | set[str], *, actor_id: str) -> None:
        agent_id = self._require_text(agent_id, "agent_id")
        actor_id = self._require_text(actor_id, "actor_id")
        normalized = self._string_tuple(tuple(capabilities), "capabilities")
        if not normalized:
            raise CapabilityError("at least one capability is required")
        timestamp = self.clock()
        with self.store.write() as conn:
            for capability in sorted(set(normalized)):
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO capability_grants(agent_id, capability, granted_by, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (agent_id, capability, actor_id, timestamp),
                )
                if cursor.rowcount:
                    self.store.append_event(
                        conn,
                        kind="capability.granted",
                        actor_id=actor_id,
                        actor_type="operator",
                        payload={"agent_id": agent_id, "capability": capability},
                        created_at=timestamp,
                    )

    def propose(
        self,
        actor_id: str,
        mission_id: str,
        action_type: str,
        target: dict[str, Any],
        arguments: dict[str, Any],
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        idempotency_key: str,
    ) -> ActionRequest:
        actor_id = self._require_text(actor_id, "actor_id")
        mission_id = self._require_text(mission_id, "mission_id")
        action_type = self._require_text(action_type, "action_type")
        idempotency_key = self._require_text(idempotency_key, "idempotency_key")
        self._reject_secret_fields(target, "target")
        self._reject_secret_fields(arguments, "arguments")
        target_json = self._json_object(target, "target")
        arguments_json = self._json_object(arguments, "arguments")
        risk_class = self.risk_policy.get(action_type)
        if risk_class is None:
            raise CapabilityError(f"action type is not present in risk policy: {action_type}")
        canonical = {
            "actor_id": actor_id,
            "mission_id": mission_id,
            "task_id": task_id,
            "run_id": run_id,
            "action_type": action_type,
            "target": json.loads(target_json),
            "arguments": json.loads(arguments_json),
            "idempotency_key": idempotency_key,
        }
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        timestamp = self.clock()

        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT * FROM action_requests WHERE mission_id = ? AND idempotency_key = ?",
                (mission_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                request = self._request_from_row(existing)
                if request.payload_hash != payload_hash:
                    raise IdempotencyConflictError("idempotency key was reused with a different payload")
                return request
            granted = conn.execute(
                "SELECT 1 FROM capability_grants WHERE agent_id = ? AND capability = ?",
                (actor_id, action_type),
            ).fetchone()
            if granted is None:
                raise CapabilityError(f"capability is not granted: {action_type}")
            self._validate_run_scope(conn, actor_id, mission_id, task_id, run_id)
            status = {
                "R0": "authorized",
                "R1": "authorized",
                "R2": "awaiting_approval",
                "R3": "awaiting_approval",
                "R4": "denied",
            }[risk_class]
            action_id = self._new_id("act")
            conn.execute(
                """
                INSERT INTO action_requests(
                    action_id, actor_id, mission_id, task_id, run_id, action_type,
                    target_json, arguments_json, risk_class, status, payload_hash,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    actor_id,
                    mission_id,
                    task_id,
                    run_id,
                    action_type,
                    target_json,
                    arguments_json,
                    risk_class,
                    status,
                    payload_hash,
                    idempotency_key,
                    timestamp,
                    timestamp,
                ),
            )
            self.store.append_event(
                conn,
                kind="action.proposed",
                actor_id=actor_id,
                mission_id=mission_id,
                task_id=task_id,
                run_id=run_id,
                payload={
                    "action_id": action_id,
                    "action_type": action_type,
                    "risk_class": risk_class,
                    "status": status,
                    "payload_hash": payload_hash,
                },
                created_at=timestamp,
            )
            return self._require_request(conn, action_id)

    def approve(self, action_id: str, approver_id: str, *, payload_hash: str) -> ActionRequest:
        approver_id = self._require_text(approver_id, "approver_id")
        timestamp = self.clock()
        with self.store.write() as conn:
            request = self._require_request(conn, action_id)
            if request.status != "awaiting_approval":
                raise ApprovalError(f"action is not authorized for approval from status {request.status}")
            if payload_hash != request.payload_hash:
                raise ApprovalError("approval payload hash does not match action")
            conn.execute(
                """
                INSERT INTO approvals(action_id, payload_hash, decision, decided_by, decided_at)
                VALUES (?, ?, 'approved', ?, ?)
                """,
                (action_id, payload_hash, approver_id, timestamp),
            )
            conn.execute(
                "UPDATE action_requests SET status = 'authorized', approved_by = ?, updated_at = ? WHERE action_id = ?",
                (approver_id, timestamp, action_id),
            )
            self.store.append_event(
                conn,
                kind="action.approved",
                actor_id=approver_id,
                actor_type="operator",
                mission_id=request.mission_id,
                task_id=request.task_id,
                run_id=request.run_id,
                payload={"action_id": action_id, "payload_hash": payload_hash},
                created_at=timestamp,
            )
            return self._require_request(conn, action_id)

    def deny(self, action_id: str, approver_id: str, *, reason: str) -> ActionRequest:
        reason = self._require_text(reason, "reason")
        timestamp = self.clock()
        with self.store.write() as conn:
            request = self._require_request(conn, action_id)
            if request.status != "awaiting_approval":
                raise ApprovalError(f"action cannot be denied from status {request.status}")
            conn.execute(
                """
                INSERT INTO approvals(action_id, payload_hash, decision, decided_by, reason, decided_at)
                VALUES (?, ?, 'denied', ?, ?, ?)
                """,
                (action_id, request.payload_hash, approver_id, reason, timestamp),
            )
            conn.execute(
                "UPDATE action_requests SET status = 'denied', updated_at = ? WHERE action_id = ?",
                (timestamp, action_id),
            )
            self.store.append_event(
                conn,
                kind="action.denied",
                actor_id=approver_id,
                actor_type="operator",
                mission_id=request.mission_id,
                task_id=request.task_id,
                run_id=request.run_id,
                payload={"action_id": action_id, "reason": reason},
                created_at=timestamp,
            )
            return self._require_request(conn, action_id)

    def execute(self, action_id: str, handlers: Mapping[str, ActionHandler]) -> ActionReceipt:
        existing = self.get_receipt(action_id)
        if existing is not None:
            return existing
        timestamp = self.clock()
        with self.store.write() as conn:
            request = self._require_request(conn, action_id)
            if request.status != "authorized":
                raise ApprovalError(f"action is not authorized for execution from status {request.status}")
            self._validate_run_scope(
                conn,
                request.actor_id,
                request.mission_id,
                request.task_id,
                request.run_id,
            )
            handler = handlers.get(request.action_type)
            if handler is None:
                raise CapabilityError(f"no handler registered for action type: {request.action_type}")
            cursor = conn.execute(
                """
                UPDATE action_requests SET status = 'executing', updated_at = ?
                WHERE action_id = ? AND status = 'authorized'
                """,
                (timestamp, action_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalError("action execution was claimed by another caller")
            self.store.append_event(
                conn,
                kind="action.executing",
                actor_id="action-broker",
                actor_type="system",
                mission_id=request.mission_id,
                task_id=request.task_id,
                run_id=request.run_id,
                payload={"action_id": action_id, "action_type": request.action_type},
                created_at=timestamp,
            )

        request = self.get_request(action_id)
        try:
            result = handler.execute(request)
            self._reject_secret_fields(result, "action result")
            result_json = self._json_object(result, "action result")
        except ActionOutcomeUnknown as exc:
            return self._record_receipt(request, "unknown", {}, {}, self._safe_error(exc))
        except Exception as exc:
            return self._record_receipt(request, "unknown", {}, {}, self._safe_error(exc))

        try:
            verification = handler.verify(request, json.loads(result_json))
            self._reject_secret_fields(verification, "action verification")
            verification_json = self._json_object(verification, "action verification")
        except ActionOutcomeUnknown as exc:
            return self._record_receipt(
                request, "unknown", json.loads(result_json), {}, self._safe_error(exc)
            )
        except Exception as exc:
            return self._record_receipt(request, "unknown", json.loads(result_json), {}, self._safe_error(exc))

        verification_data = json.loads(verification_json)
        if not isinstance(verification_data.get("verified"), bool):
            return self._record_receipt(
                request,
                "unknown",
                json.loads(result_json),
                verification_data,
                "verification must include a boolean 'verified'",
            )
        status = "verified" if verification_data["verified"] else "unverified"
        return self._record_receipt(
            request,
            status,
            json.loads(result_json),
            verification_data,
            None if status == "verified" else "target readback did not verify the action",
        )

    def get_request(self, action_id: str) -> ActionRequest:
        with self.store.read() as conn:
            return self._require_request(conn, action_id)

    def get_receipt(self, action_id: str) -> ActionReceipt | None:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM action_receipts WHERE action_id = ?", (action_id,)).fetchone()
        return None if row is None else self._receipt_from_row(row)

    def recover_executing(self, actor_id: str) -> list[ActionReceipt]:
        actor_id = self._require_text(actor_id, "actor_id")
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM action_requests WHERE status = 'executing' ORDER BY created_at, action_id"
            ).fetchall()
        receipts: list[ActionReceipt] = []
        for row in rows:
            request = self._request_from_row(row)
            receipts.append(
                self._record_receipt(
                    request,
                    "unknown",
                    {},
                    {},
                    "coordinator restarted before the external outcome was verified",
                    actor_id=actor_id,
                )
            )
        return receipts

    def _record_receipt(
        self,
        request: ActionRequest,
        status: str,
        result: dict[str, Any],
        verification: dict[str, Any],
        error: str | None,
        *,
        actor_id: str = "action-broker",
    ) -> ActionReceipt:
        timestamp = self.clock()
        receipt_id = self._new_id("receipt")
        result_json = self._json_object(result, "action result")
        verification_json = self._json_object(verification, "action verification")
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT * FROM action_receipts WHERE action_id = ?", (request.action_id,)
            ).fetchone()
            if existing is not None:
                return self._receipt_from_row(existing)
            conn.execute(
                """
                INSERT INTO action_receipts(
                    receipt_id, action_id, status, result_json, verification_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (receipt_id, request.action_id, status, result_json, verification_json, error, timestamp),
            )
            conn.execute(
                "UPDATE action_requests SET status = ?, updated_at = ? WHERE action_id = ? AND status = 'executing'",
                (status, timestamp, request.action_id),
            )
            self.store.append_event(
                conn,
                kind=f"action.{status}",
                actor_id=actor_id,
                actor_type="system",
                mission_id=request.mission_id,
                task_id=request.task_id,
                run_id=request.run_id,
                payload={"action_id": request.action_id, "receipt_id": receipt_id, "error": error},
                created_at=timestamp,
            )
            row = conn.execute(
                "SELECT * FROM action_receipts WHERE action_id = ?", (request.action_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("action receipt could not be read back")
            return self._receipt_from_row(row)

    def _validate_run_scope(
        self,
        conn: sqlite3.Connection,
        actor_id: str,
        mission_id: str,
        task_id: str | None,
        run_id: str | None,
    ) -> None:
        if (task_id is None) != (run_id is None):
            raise CapabilityError("task_id and run_id must be provided together")
        if task_id is None:
            return
        row = conn.execute(
            """
            SELECT t.status, t.current_run_id, t.mission_id, t.lease_expires_at,
                   t.max_runtime_seconds, r.worker_id, r.status AS run_status,
                   r.started_at
            FROM tasks t
            LEFT JOIN task_runs r ON r.run_id = t.current_run_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        timestamp = self.clock()
        if (
            row is None
            or row["status"] != "running"
            or row["run_status"] != "running"
            or row["current_run_id"] != run_id
            or row["mission_id"] != mission_id
            or row["worker_id"] != actor_id
            or row["lease_expires_at"] is None
            or float(row["lease_expires_at"]) <= timestamp
            or row["started_at"] is None
            or float(row["started_at"]) + float(row["max_runtime_seconds"]) <= timestamp
        ):
            raise CapabilityError("action is not associated with the actor's current active run")

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> ActionRequest:
        return ActionRequest(
            action_id=str(row["action_id"]),
            actor_id=str(row["actor_id"]),
            mission_id=str(row["mission_id"]),
            task_id=row["task_id"],
            run_id=row["run_id"],
            action_type=str(row["action_type"]),
            target=json.loads(row["target_json"]),
            arguments=json.loads(row["arguments_json"]),
            risk_class=str(row["risk_class"]),
            status=str(row["status"]),
            payload_hash=str(row["payload_hash"]),
            idempotency_key=str(row["idempotency_key"]),
            approved_by=row["approved_by"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _require_request(self, conn: sqlite3.Connection, action_id: str) -> ActionRequest:
        row = conn.execute("SELECT * FROM action_requests WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise CapabilityError(f"unknown action: {action_id}")
        return self._request_from_row(row)

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> ActionReceipt:
        return ActionReceipt(
            receipt_id=str(row["receipt_id"]),
            action_id=str(row["action_id"]),
            status=str(row["status"]),
            result=json.loads(row["result_json"]),
            verification=json.loads(row["verification_json"]),
            error=row["error"],
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _require_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be empty")
        return value.strip()

    @staticmethod
    def _string_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError(f"{field_name} must be a sequence of strings")
        normalized = tuple(values)
        if not all(isinstance(value, str) and value for value in normalized):
            raise ValueError(f"{field_name} must be a sequence of non-empty strings")
        return normalized

    @staticmethod
    def _json_object(value: dict[str, Any], field_name: str) -> str:
        if not isinstance(value, dict):
            raise ValueError(f"{field_name} must be an object")
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be JSON serializable") from exc

    @staticmethod
    def _reject_secret_fields(value: Any, field_name: str) -> None:
        seen: set[int] = set()

        def visit(current: Any, path: str) -> None:
            if isinstance(current, dict):
                identity = id(current)
                if identity in seen:
                    return
                seen.add(identity)
                for key, nested in current.items():
                    if isinstance(key, str):
                        normalized = key.strip().lower().replace("-", "_")
                        if normalized in _SENSITIVE_FIELD_NAMES:
                            raise CapabilityError(
                                f"secret field is not allowed in action payload: {path}.{key}"
                            )
                    visit(nested, f"{path}.{key}")
            elif isinstance(current, (list, tuple)):
                identity = id(current)
                if identity in seen:
                    return
                seen.add(identity)
                for index, nested in enumerate(current):
                    visit(nested, f"{path}[{index}]")

        visit(value, field_name)

    def _safe_error(self, exc: Exception) -> str:
        return self.redactor.text(f"{type(exc).__name__}: {exc}")


class BudgetService:
    """Atomic reservations and accounting for mission, task, or agent budgets."""

    def __init__(self, store: SQLiteStore, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        with self.store.connect() as conn:
            conn.executescript(_ACTION_SCHEMA)

    def set_limit(
        self,
        scope_type: str,
        scope_id: str,
        unit: str,
        limit_value: float,
        *,
        actor_id: str,
    ) -> Budget:
        self._validate_amount(limit_value, "limit_value")
        timestamp = self.clock()
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT * FROM budgets WHERE scope_type = ? AND scope_id = ? AND unit = ?",
                (scope_type, scope_id, unit),
            ).fetchone()
            if existing is not None and (
                float(existing["used_value"]) + float(existing["reserved_value"]) > limit_value
            ):
                raise BudgetExceededError("new budget limit is below current usage and reservations")
            conn.execute(
                """
                INSERT INTO budgets(scope_type, scope_id, unit, limit_value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_id, unit)
                DO UPDATE SET limit_value = excluded.limit_value, updated_at = excluded.updated_at
                """,
                (scope_type, scope_id, unit, float(limit_value), timestamp),
            )
            self.store.append_event(
                conn,
                kind="budget.configured",
                actor_id=actor_id,
                actor_type="operator",
                payload={"scope_type": scope_type, "scope_id": scope_id, "unit": unit, "limit": limit_value},
                created_at=timestamp,
            )
            return self._require_budget(conn, scope_type, scope_id, unit)

    def get(self, scope_type: str, scope_id: str, unit: str) -> Budget:
        with self.store.read() as conn:
            return self._require_budget(conn, scope_type, scope_id, unit)

    def charge_if_configured(
        self,
        scope_type: str,
        scope_id: str,
        unit: str,
        amount: float,
        reservation_id: str,
    ) -> BudgetReservation | None:
        self._validate_amount(amount, "amount")
        timestamp = self.clock()
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if existing is not None:
                reservation = self._reservation_from_row(existing)
                if (
                    reservation.scope_type != scope_type
                    or reservation.scope_id != scope_id
                    or reservation.unit != unit
                    or reservation.amount != float(amount)
                    or reservation.status != "consumed"
                ):
                    raise IdempotencyConflictError(
                        "reservation id was reused with different charge data"
                    )
                return reservation
            row = conn.execute(
                "SELECT * FROM budgets WHERE scope_type = ? AND scope_id = ? AND unit = ?",
                (scope_type, scope_id, unit),
            ).fetchone()
            if row is None:
                return None
            budget = self._require_budget(conn, scope_type, scope_id, unit)
            if budget.used_value + budget.reserved_value + amount > budget.limit_value:
                raise BudgetExceededError(
                    f"budget exceeded for {scope_type}:{scope_id}:{unit}"
                )
            conn.execute(
                """
                INSERT INTO budget_reservations(
                    reservation_id, scope_type, scope_id, unit, amount, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'consumed', ?, ?)
                """,
                (
                    reservation_id,
                    scope_type,
                    scope_id,
                    unit,
                    float(amount),
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                UPDATE budgets SET used_value = used_value + ?, updated_at = ?
                WHERE scope_type = ? AND scope_id = ? AND unit = ?
                """,
                (float(amount), timestamp, scope_type, scope_id, unit),
            )
            self.store.append_event(
                conn,
                kind="budget.consumed",
                actor_id="budget-service",
                actor_type="system",
                payload={"reservation_id": reservation_id, "amount": amount, "unit": unit},
                created_at=timestamp,
            )
            return self._require_reservation(conn, reservation_id)

    def reserve(
        self,
        scope_type: str,
        scope_id: str,
        unit: str,
        amount: float,
        reservation_id: str,
    ) -> BudgetReservation:
        self._validate_amount(amount, "amount")
        timestamp = self.clock()
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?", (reservation_id,)
            ).fetchone()
            if existing is not None:
                reservation = self._reservation_from_row(existing)
                if (
                    reservation.scope_type != scope_type
                    or reservation.scope_id != scope_id
                    or reservation.unit != unit
                    or reservation.amount != float(amount)
                ):
                    raise IdempotencyConflictError("reservation id was reused with different budget data")
                return reservation
            budget = self._require_budget(conn, scope_type, scope_id, unit)
            if budget.used_value + budget.reserved_value + amount > budget.limit_value:
                raise BudgetExceededError(
                    f"budget exceeded for {scope_type}:{scope_id}:{unit}"
                )
            conn.execute(
                """
                INSERT INTO budget_reservations(
                    reservation_id, scope_type, scope_id, unit, amount, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (reservation_id, scope_type, scope_id, unit, float(amount), timestamp, timestamp),
            )
            conn.execute(
                """
                UPDATE budgets SET reserved_value = reserved_value + ?, updated_at = ?
                WHERE scope_type = ? AND scope_id = ? AND unit = ?
                """,
                (float(amount), timestamp, scope_type, scope_id, unit),
            )
            self.store.append_event(
                conn,
                kind="budget.reserved",
                actor_id="budget-service",
                actor_type="system",
                payload={"reservation_id": reservation_id, "amount": amount, "unit": unit},
                created_at=timestamp,
            )
            return self._require_reservation(conn, reservation_id)

    def consume(self, reservation_id: str) -> BudgetReservation:
        return self._finish_reservation(reservation_id, "consumed")

    def release(self, reservation_id: str) -> BudgetReservation:
        return self._finish_reservation(reservation_id, "released")

    def get_reservation(self, reservation_id: str) -> BudgetReservation:
        with self.store.read() as conn:
            return self._require_reservation(conn, reservation_id)

    def _finish_reservation(self, reservation_id: str, target_status: str) -> BudgetReservation:
        timestamp = self.clock()
        with self.store.write() as conn:
            reservation = self._require_reservation(conn, reservation_id)
            if reservation.status == target_status:
                return reservation
            if reservation.status != "active":
                raise BudgetExceededError(
                    f"reservation cannot transition from {reservation.status} to {target_status}"
                )
            used_increment = reservation.amount if target_status == "consumed" else 0.0
            conn.execute(
                """
                UPDATE budgets
                SET reserved_value = reserved_value - ?, used_value = used_value + ?, updated_at = ?
                WHERE scope_type = ? AND scope_id = ? AND unit = ?
                """,
                (
                    reservation.amount,
                    used_increment,
                    timestamp,
                    reservation.scope_type,
                    reservation.scope_id,
                    reservation.unit,
                ),
            )
            conn.execute(
                "UPDATE budget_reservations SET status = ?, updated_at = ? WHERE reservation_id = ?",
                (target_status, timestamp, reservation_id),
            )
            self.store.append_event(
                conn,
                kind=f"budget.{target_status}",
                actor_id="budget-service",
                actor_type="system",
                payload={"reservation_id": reservation_id, "amount": reservation.amount},
                created_at=timestamp,
            )
            return self._require_reservation(conn, reservation_id)

    @staticmethod
    def _validate_amount(value: float, field_name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{field_name} must be a positive finite number")

    @staticmethod
    def _require_budget(conn: sqlite3.Connection, scope_type: str, scope_id: str, unit: str) -> Budget:
        row = conn.execute(
            "SELECT * FROM budgets WHERE scope_type = ? AND scope_id = ? AND unit = ?",
            (scope_type, scope_id, unit),
        ).fetchone()
        if row is None:
            raise BudgetExceededError(f"budget is not configured for {scope_type}:{scope_id}:{unit}")
        return Budget(
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            unit=str(row["unit"]),
            limit_value=float(row["limit_value"]),
            used_value=float(row["used_value"]),
            reserved_value=float(row["reserved_value"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _require_reservation(conn: sqlite3.Connection, reservation_id: str) -> BudgetReservation:
        row = conn.execute(
            "SELECT * FROM budget_reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise BudgetExceededError(f"unknown budget reservation: {reservation_id}")
        return BudgetService._reservation_from_row(row)

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> BudgetReservation:
        return BudgetReservation(
            reservation_id=str(row["reservation_id"]),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            unit=str(row["unit"]),
            amount=float(row["amount"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
