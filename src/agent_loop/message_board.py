from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .persistence import SQLiteStore


MESSAGE_KINDS = frozenset(
    {
        "fact_observation",
        "hypothesis",
        "question",
        "response",
        "request",
        "decision_proposal",
        "decision_notice",
        "checkpoint",
        "conflict",
        "hotspot",
        "review_note",
        "verdict",
        "alert",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")


class MessageValidationError(ValueError):
    """Raised when a message or subscription violates the board protocol."""


class FactConflictError(RuntimeError):
    """Raised when a compare-and-swap fact update uses a stale version."""


class FactOwnershipError(PermissionError):
    """Raised when a peer attempts to overwrite another actor's fact namespace."""


@dataclass(frozen=True)
class Message:
    sequence: int
    message_id: str
    mission_id: str
    task_id: str | None
    topic: str
    kind: str
    actor_id: str
    recipients: tuple[str, ...]
    correlation_id: str | None
    reply_to: str | None
    subject: str
    body: str
    data: dict[str, Any]
    artifact_refs: tuple[str, ...]
    dedupe_key: str | None
    created_at: float
    expires_at: float | None


@dataclass(frozen=True)
class Subscription:
    subscription_id: str
    mission_id: str
    subscriber_id: str
    topic_prefix: str
    cursor_sequence: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Fact:
    mission_id: str
    fact_key: str
    value: Any
    owner_id: str
    version: int
    created_at: float
    updated_at: float


class MessageBoard:
    """Typed append-only messages plus compare-and-swap shared facts."""

    def __init__(self, store: SQLiteStore, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def publish(
        self,
        mission_id: str,
        topic: str,
        kind: str,
        actor_id: str,
        body: str,
        *,
        task_id: str | None = None,
        recipients: Sequence[str] = (),
        correlation_id: str | None = None,
        reply_to: str | None = None,
        subject: str = "",
        data: dict[str, Any] | None = None,
        artifact_refs: Sequence[str] = (),
        dedupe_key: str | None = None,
        expires_at: float | None = None,
    ) -> Message:
        self._validate_scope(mission_id, topic)
        self._require_identifier(actor_id, "actor_id")
        if kind not in MESSAGE_KINDS:
            raise MessageValidationError(f"unknown message kind: {kind}")
        if not isinstance(body, str):
            raise MessageValidationError("message body must be a string")
        if not isinstance(subject, str):
            raise MessageValidationError("message subject must be a string")
        normalized_recipients = self._string_tuple(recipients, "recipients")
        normalized_artifacts = self._string_tuple(artifact_refs, "artifact_refs")
        payload = {} if data is None else data
        if not isinstance(payload, dict):
            raise MessageValidationError("message data must be an object")
        payload_json = self._json(payload, "message data")
        recipients_json = self._json(list(normalized_recipients), "recipients")
        artifacts_json = self._json(list(normalized_artifacts), "artifact_refs")
        if dedupe_key is not None:
            dedupe_key = self._require_text(dedupe_key, "dedupe_key")
        timestamp = self.clock()

        with self.store.write() as conn:
            if dedupe_key is not None:
                row = conn.execute(
                    "SELECT * FROM messages WHERE mission_id = ? AND dedupe_key = ?",
                    (mission_id, dedupe_key),
                ).fetchone()
                if row is not None:
                    message = self._message_from_row(row)
                    if (
                        message.task_id != task_id
                        or message.topic != topic
                        or message.kind != kind
                        or message.actor_id != actor_id
                        or row["recipients_json"] != recipients_json
                        or message.correlation_id != correlation_id
                        or message.reply_to != reply_to
                        or message.subject != subject
                        or message.body != body
                        or row["data_json"] != payload_json
                        or row["artifact_refs_json"] != artifacts_json
                        or message.expires_at != expires_at
                    ):
                        raise MessageValidationError(
                            "message idempotency key was reused with a different payload"
                        )
                    return message
            if reply_to is not None:
                parent = conn.execute(
                    "SELECT mission_id FROM messages WHERE message_id = ?", (reply_to,)
                ).fetchone()
                if parent is None:
                    raise MessageValidationError(f"reply_to message does not exist: {reply_to}")
                if parent["mission_id"] != mission_id:
                    raise MessageValidationError("reply_to message belongs to another mission")

            message_id = self._new_id("msg")
            cursor = conn.execute(
                """
                INSERT INTO messages(
                    message_id, mission_id, task_id, topic, kind, actor_id,
                    recipients_json, correlation_id, reply_to, subject, body,
                    data_json, artifact_refs_json, dedupe_key, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    mission_id,
                    task_id,
                    topic,
                    kind,
                    actor_id,
                    recipients_json,
                    correlation_id,
                    reply_to,
                    subject,
                    body,
                    payload_json,
                    artifacts_json,
                    dedupe_key,
                    timestamp,
                    expires_at,
                ),
            )
            sequence = int(cursor.lastrowid or 0)
            self.store.append_event(
                conn,
                kind="message.published",
                actor_id=actor_id,
                mission_id=mission_id,
                task_id=task_id,
                correlation_id=correlation_id,
                payload={"message_id": message_id, "sequence": sequence, "topic": topic, "kind": kind},
                created_at=timestamp,
            )
            row = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
            if row is None:
                raise RuntimeError("published message could not be read back")
            return self._message_from_row(row)

    def list_messages(
        self,
        mission_id: str,
        *,
        topic_prefix: str | None = None,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> list[Message]:
        self._require_identifier(mission_id, "mission_id")
        if limit < 1:
            raise MessageValidationError("limit must be positive")
        query = "SELECT * FROM messages WHERE mission_id = ? AND sequence > ?"
        params: list[Any] = [mission_id, after_sequence]
        if topic_prefix is not None:
            self._validate_scope(mission_id, topic_prefix)
            query += " AND topic GLOB ?"
            params.append(f"{topic_prefix}*")
        query += " AND (expires_at IS NULL OR expires_at > ?) ORDER BY sequence LIMIT ?"
        params.extend([self.clock(), limit])
        with self.store.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._message_from_row(row) for row in rows]

    def subscribe(self, mission_id: str, subscriber_id: str, topic_prefix: str) -> Subscription:
        self._validate_scope(mission_id, topic_prefix)
        self._require_identifier(subscriber_id, "subscriber_id")
        timestamp = self.clock()
        with self.store.write() as conn:
            row = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE mission_id = ? AND subscriber_id = ? AND topic_prefix = ?
                """,
                (mission_id, subscriber_id, topic_prefix),
            ).fetchone()
            if row is not None:
                return self._subscription_from_row(row)
            subscription_id = self._new_id("sub")
            conn.execute(
                """
                INSERT INTO subscriptions(
                    subscription_id, mission_id, subscriber_id, topic_prefix,
                    cursor_sequence, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (subscription_id, mission_id, subscriber_id, topic_prefix, timestamp, timestamp),
            )
            self.store.append_event(
                conn,
                kind="subscription.created",
                actor_id=subscriber_id,
                mission_id=mission_id,
                payload={"subscription_id": subscription_id, "topic_prefix": topic_prefix},
                created_at=timestamp,
            )
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("subscription could not be read back")
            return self._subscription_from_row(row)

    def get_subscription(self, subscription_id: str) -> Subscription:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
        if row is None:
            raise MessageValidationError(f"unknown subscription: {subscription_id}")
        return self._subscription_from_row(row)

    def read_subscription(self, subscription_id: str, *, limit: int = 100) -> list[Message]:
        if limit < 1:
            raise MessageValidationError("limit must be positive")
        subscription = self.get_subscription(subscription_id)
        with self.store.read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE mission_id = ? AND topic GLOB ? AND sequence > ?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY sequence LIMIT ?
                """,
                (
                    subscription.mission_id,
                    f"{subscription.topic_prefix}*",
                    subscription.cursor_sequence,
                    self.clock(),
                    limit,
                ),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def ack(self, subscription_id: str, message_id: str) -> Subscription:
        timestamp = self.clock()
        with self.store.write() as conn:
            subscription_row = conn.execute(
                "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
            if subscription_row is None:
                raise MessageValidationError(f"unknown subscription: {subscription_id}")
            message_row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if message_row is None:
                raise MessageValidationError(f"unknown message: {message_id}")
            if (
                message_row["mission_id"] != subscription_row["mission_id"]
                or not str(message_row["topic"]).startswith(str(subscription_row["topic_prefix"]))
            ):
                raise MessageValidationError("message is outside subscription scope")
            new_cursor = max(int(subscription_row["cursor_sequence"]), int(message_row["sequence"]))
            conn.execute(
                "UPDATE subscriptions SET cursor_sequence = ?, updated_at = ? WHERE subscription_id = ?",
                (new_cursor, timestamp, subscription_id),
            )
            if new_cursor != int(subscription_row["cursor_sequence"]):
                self.store.append_event(
                    conn,
                    kind="message.acked",
                    actor_id=str(subscription_row["subscriber_id"]),
                    mission_id=str(subscription_row["mission_id"]),
                    payload={"subscription_id": subscription_id, "message_id": message_id, "sequence": new_cursor},
                    created_at=timestamp,
                )
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("subscription disappeared during acknowledgement")
            return self._subscription_from_row(row)

    def put_fact(
        self,
        mission_id: str,
        fact_key: str,
        value: Any,
        actor_id: str,
        *,
        expected_version: int,
    ) -> Fact:
        self._require_identifier(mission_id, "mission_id")
        self._require_text(fact_key, "fact_key")
        self._require_identifier(actor_id, "actor_id")
        if expected_version < 0:
            raise MessageValidationError("expected_version must not be negative")
        value_json = self._json(value, "fact value")
        timestamp = self.clock()

        with self.store.write() as conn:
            row = conn.execute(
                "SELECT * FROM facts WHERE mission_id = ? AND fact_key = ?",
                (mission_id, fact_key),
            ).fetchone()
            if row is None:
                if expected_version != 0:
                    raise FactConflictError(
                        f"fact {fact_key} expected version {expected_version}, current version is 0"
                    )
                version = 1
                conn.execute(
                    """
                    INSERT INTO facts(
                        mission_id, fact_key, value_json, owner_id, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mission_id, fact_key, value_json, actor_id, version, timestamp, timestamp),
                )
            else:
                owner_id = str(row["owner_id"])
                if owner_id != actor_id:
                    raise FactOwnershipError(f"fact {fact_key} is owned by {owner_id}")
                current_version = int(row["version"])
                if current_version != expected_version:
                    raise FactConflictError(
                        f"fact {fact_key} expected version {expected_version}, current version is {current_version}"
                    )
                version = current_version + 1
                cursor = conn.execute(
                    """
                    UPDATE facts SET value_json = ?, version = ?, updated_at = ?
                    WHERE mission_id = ? AND fact_key = ? AND version = ?
                    """,
                    (value_json, version, timestamp, mission_id, fact_key, current_version),
                )
                if cursor.rowcount != 1:
                    raise FactConflictError(f"fact {fact_key} changed during update")
            self.store.append_event(
                conn,
                kind="fact.committed",
                actor_id=actor_id,
                mission_id=mission_id,
                payload={"fact_key": fact_key, "version": version},
                created_at=timestamp,
            )
            stored = conn.execute(
                "SELECT * FROM facts WHERE mission_id = ? AND fact_key = ?",
                (mission_id, fact_key),
            ).fetchone()
            if stored is None:
                raise RuntimeError("fact could not be read back")
            return self._fact_from_row(stored)

    def get_fact(self, mission_id: str, fact_key: str) -> Fact | None:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM facts WHERE mission_id = ? AND fact_key = ?",
                (mission_id, fact_key),
            ).fetchone()
        return None if row is None else self._fact_from_row(row)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _json(value: Any, field_name: str) -> str:
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise MessageValidationError(f"{field_name} must be JSON serializable") from exc

    @staticmethod
    def _string_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise MessageValidationError(f"{field_name} must be a sequence of strings")
        normalized = tuple(values)
        if not all(isinstance(value, str) and value for value in normalized):
            raise MessageValidationError(f"{field_name} must be a sequence of non-empty strings")
        return normalized

    @classmethod
    def _validate_scope(cls, mission_id: str, topic: str) -> None:
        cls._require_identifier(mission_id, "mission_id")
        cls._require_identifier(topic, "topic")
        if not topic.startswith(f"mission.{mission_id}."):
            raise MessageValidationError(f"topic must be scoped to mission {mission_id}")

    @staticmethod
    def _require_identifier(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise MessageValidationError(f"{field_name} must be a valid identifier")

    @staticmethod
    def _require_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MessageValidationError(f"{field_name} must not be empty")
        return value.strip()

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        return Message(
            sequence=int(row["sequence"]),
            message_id=str(row["message_id"]),
            mission_id=str(row["mission_id"]),
            task_id=row["task_id"],
            topic=str(row["topic"]),
            kind=str(row["kind"]),
            actor_id=str(row["actor_id"]),
            recipients=tuple(json.loads(row["recipients_json"])),
            correlation_id=row["correlation_id"],
            reply_to=row["reply_to"],
            subject=str(row["subject"]),
            body=str(row["body"]),
            data=json.loads(row["data_json"]),
            artifact_refs=tuple(json.loads(row["artifact_refs_json"])),
            dedupe_key=row["dedupe_key"],
            created_at=float(row["created_at"]),
            expires_at=None if row["expires_at"] is None else float(row["expires_at"]),
        )

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> Subscription:
        return Subscription(
            subscription_id=str(row["subscription_id"]),
            mission_id=str(row["mission_id"]),
            subscriber_id=str(row["subscriber_id"]),
            topic_prefix=str(row["topic_prefix"]),
            cursor_sequence=int(row["cursor_sequence"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> Fact:
        return Fact(
            mission_id=str(row["mission_id"]),
            fact_key=str(row["fact_key"]),
            value=json.loads(row["value_json"]),
            owner_id=str(row["owner_id"]),
            version=int(row["version"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
