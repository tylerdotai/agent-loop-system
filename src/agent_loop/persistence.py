from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class Event:
    event_id: int
    kind: str
    actor_id: str
    actor_type: str
    source: str
    mission_id: str | None
    task_id: str | None
    run_id: str | None
    correlation_id: str | None
    payload: dict[str, Any]
    created_at: float


_BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_meta(component, version) VALUES ('control_plane', 1);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    source TEXT NOT NULL,
    mission_id TEXT,
    task_id TEXT,
    run_id TEXT,
    correlation_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind_id ON events(kind, id);
CREATE INDEX IF NOT EXISTS idx_events_mission_id ON events(mission_id, id);

CREATE TABLE IF NOT EXISTS messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    mission_id TEXT NOT NULL,
    task_id TEXT,
    topic TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    correlation_id TEXT,
    reply_to TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    data_json TEXT NOT NULL,
    artifact_refs_json TEXT NOT NULL,
    dedupe_key TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    FOREIGN KEY(reply_to) REFERENCES messages(message_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_mission_dedupe
    ON messages(mission_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_mission_sequence
    ON messages(mission_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_topic_sequence
    ON messages(topic, sequence);

CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    subscriber_id TEXT NOT NULL,
    topic_prefix TEXT NOT NULL,
    cursor_sequence INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(mission_id, subscriber_id, topic_prefix)
);

CREATE TABLE IF NOT EXISTS facts (
    mission_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(mission_id, fact_key)
);
"""


class SQLiteStore:
    """SQLite event store shared by the multi-agent control-plane services."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(_BOARD_SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        for database_file in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if database_file.exists():
                os.chmod(database_file, 0o600)
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_event(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        actor_id: str,
        payload: dict[str, Any] | None = None,
        actor_type: str = "agent",
        source: str = "control_plane",
        mission_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        correlation_id: str | None = None,
        created_at: float | None = None,
    ) -> Event:
        timestamp = time.time() if created_at is None else created_at
        payload_json = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        cursor = conn.execute(
            """
            INSERT INTO events(
                kind, actor_id, actor_type, source, mission_id, task_id, run_id,
                correlation_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                actor_id,
                actor_type,
                source,
                mission_id,
                task_id,
                run_id,
                correlation_id,
                payload_json,
                timestamp,
            ),
        )
        event_id = cursor.lastrowid
        if event_id is None:
            raise RuntimeError("event insert did not return an id")
        return Event(
            event_id=int(event_id),
            kind=kind,
            actor_id=actor_id,
            actor_type=actor_type,
            source=source,
            mission_id=mission_id,
            task_id=task_id,
            run_id=run_id,
            correlation_id=correlation_id,
            payload=json.loads(payload_json),
            created_at=timestamp,
        )

    def list_events(self, *, kind: str | None = None, after_id: int = 0, limit: int = 1_000) -> list[Event]:
        if limit < 1:
            raise ValueError("limit must be positive")
        query = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after_id]
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            event_id=int(row["id"]),
            kind=str(row["kind"]),
            actor_id=str(row["actor_id"]),
            actor_type=str(row["actor_type"]),
            source=str(row["source"]),
            mission_id=row["mission_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            correlation_id=row["correlation_id"],
            payload=json.loads(row["payload_json"]),
            created_at=float(row["created_at"]),
        )
