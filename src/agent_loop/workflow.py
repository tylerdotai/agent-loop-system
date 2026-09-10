from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Sequence

from .persistence import SQLiteStore


class WorkflowError(RuntimeError):
    """Base error for deterministic workflow state transitions."""


class MissionStateError(WorkflowError):
    """Raised when a mission transition is not valid from its current state."""


class DependencyCycleError(WorkflowError):
    """Raised when a task dependency would make the graph cyclic."""


class LeaseError(WorkflowError):
    """Raised when a worker no longer owns the current task run."""


@dataclass(frozen=True)
class Mission:
    mission_id: str
    goal: str
    state: str
    created_by: str
    limits: dict[str, Any]
    idempotency_key: str | None
    pause_reason: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Task:
    task_id: str
    mission_id: str
    title: str
    specification: dict[str, Any]
    acceptance: dict[str, Any]
    assignee: str
    status: str
    priority: int
    scheduled_at: float | None
    max_runtime_seconds: float
    max_attempts: int
    attempts: int
    idempotency_key: str | None
    resources: tuple[str, ...]
    lease_owner: str | None
    lease_expires_at: float | None
    current_run_id: str | None
    completion_summary: str | None
    completion_metadata: dict[str, Any]
    version: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class TaskRun:
    run_id: str
    task_id: str
    mission_id: str
    worker_id: str
    status: str
    outcome: str | None
    attempt: int
    lease_expires_at: float | None
    last_heartbeat_at: float | None
    started_at: float
    ended_at: float | None
    summary: str | None
    metadata: dict[str, Any]
    error: str | None


@dataclass(frozen=True)
class TaskClaim:
    task: Task
    run: TaskRun


_WORKFLOW_SCHEMA = """
INSERT OR IGNORE INTO schema_meta(component, version) VALUES ('workflow', 1);

CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    idempotency_key TEXT,
    pause_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_missions_idempotency
    ON missions(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    title TEXT NOT NULL,
    specification_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    assignee TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    scheduled_at REAL,
    max_runtime_seconds REAL NOT NULL,
    max_attempts INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    lease_owner TEXT,
    lease_expires_at REAL,
    current_run_id TEXT,
    completion_summary TEXT,
    completion_metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(mission_id) REFERENCES missions(mission_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_mission_idempotency
    ON tasks(mission_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_dispatch
    ON tasks(status, assignee, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS task_dependencies (
    parent_task_id TEXT NOT NULL,
    child_task_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(parent_task_id, child_task_id),
    FOREIGN KEY(parent_task_id) REFERENCES tasks(task_id),
    FOREIGN KEY(child_task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS task_resources (
    task_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    PRIMARY KEY(task_id, resource_key),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    attempt INTEGER NOT NULL,
    lease_expires_at REAL,
    last_heartbeat_at REAL,
    started_at REAL NOT NULL,
    ended_at REAL,
    summary TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
    FOREIGN KEY(mission_id) REFERENCES missions(mission_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_one_running
    ON task_runs(task_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_task_runs_task_started
    ON task_runs(task_id, started_at);

CREATE TABLE IF NOT EXISTS resource_leases (
    resource_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
    FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
);
"""


class WorkflowService:
    """Durable missions, dependency graphs, claims, leases, and run handoffs."""

    def __init__(self, store: SQLiteStore, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        with self.store.connect() as conn:
            conn.executescript(_WORKFLOW_SCHEMA)

    def create_mission(
        self,
        goal: str,
        actor_id: str,
        *,
        state: str = "draft",
        limits: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Mission:
        goal = self._require_text(goal, "goal")
        actor_id = self._require_text(actor_id, "actor_id")
        if state not in {"draft", "active"}:
            raise MissionStateError("mission initial state must be draft or active")
        limits_json = self._json_object(limits or {}, "limits")
        timestamp = self.clock()
        with self.store.write() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT * FROM missions WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    mission = self._mission_from_row(existing)
                    event = conn.execute(
                        """
                        SELECT actor_id, payload_json FROM events
                        WHERE kind = 'mission.created' AND mission_id = ?
                        ORDER BY id LIMIT 1
                        """,
                        (mission.mission_id,),
                    ).fetchone()
                    initial_state = (
                        None if event is None else json.loads(event["payload_json"]).get("state")
                    )
                    if (
                        mission.goal != goal
                        or mission.created_by != actor_id
                        or mission.limits != json.loads(limits_json)
                        or initial_state != state
                    ):
                        raise WorkflowError(
                            "mission idempotency key was reused with a different creation payload"
                        )
                    return mission
            mission_id = self._new_id("mis")
            conn.execute(
                """
                INSERT INTO missions(
                    mission_id, goal, state, created_by, limits_json,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mission_id, goal, state, actor_id, limits_json, idempotency_key, timestamp, timestamp),
            )
            self.store.append_event(
                conn,
                kind="mission.created",
                actor_id=actor_id,
                actor_type="operator" if actor_id == "operator" else "agent",
                mission_id=mission_id,
                payload={"state": state, "idempotency_key": idempotency_key},
                created_at=timestamp,
            )
            row = conn.execute("SELECT * FROM missions WHERE mission_id = ?", (mission_id,)).fetchone()
            if row is None:
                raise RuntimeError("mission could not be read back")
            return self._mission_from_row(row)

    def get_mission(self, mission_id: str) -> Mission:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM missions WHERE mission_id = ?", (mission_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"unknown mission: {mission_id}")
        return self._mission_from_row(row)

    def list_missions(self, *, state: str | None = None) -> list[Mission]:
        query = "SELECT * FROM missions"
        params: list[Any] = []
        if state is not None:
            query += " WHERE state = ?"
            params.append(state)
        query += " ORDER BY created_at, mission_id"
        with self.store.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._mission_from_row(row) for row in rows]

    def activate_mission(self, mission_id: str, actor_id: str) -> Mission:
        return self._transition_mission(mission_id, "draft", "active", actor_id, "mission.activated")

    def pause_mission(self, mission_id: str, actor_id: str, *, reason: str) -> Mission:
        reason = self._require_text(reason, "reason")
        timestamp = self.clock()
        with self.store.write() as conn:
            mission = self._require_mission(conn, mission_id)
            if mission.state != "active":
                raise MissionStateError(f"cannot pause mission from {mission.state}")
            conn.execute(
                "UPDATE missions SET state = 'paused', pause_reason = ?, updated_at = ? WHERE mission_id = ?",
                (reason, timestamp, mission_id),
            )
            self.store.append_event(
                conn,
                kind="mission.paused",
                actor_id=actor_id,
                actor_type="operator",
                mission_id=mission_id,
                payload={"reason": reason},
                created_at=timestamp,
            )
            return self._require_mission(conn, mission_id)

    def resume_mission(self, mission_id: str, actor_id: str) -> Mission:
        actor_id = self._require_text(actor_id, "actor_id")
        timestamp = self.clock()
        with self.store.write() as conn:
            mission = self._require_mission(conn, mission_id)
            if mission.state != "paused":
                raise MissionStateError(f"cannot resume mission from {mission.state}")
            conn.execute(
                "UPDATE missions SET state = 'active', pause_reason = NULL, updated_at = ? WHERE mission_id = ?",
                (timestamp, mission_id),
            )
            self.store.append_event(
                conn,
                kind="mission.resumed",
                actor_id=actor_id,
                actor_type="operator",
                mission_id=mission_id,
                payload={},
                created_at=timestamp,
            )
            self._promote_ready_mission_tasks(conn, mission_id, timestamp)
            return self._require_mission(conn, mission_id)

    def cancel_mission(self, mission_id: str, actor_id: str, *, reason: str) -> Mission:
        actor_id = self._require_text(actor_id, "actor_id")
        reason = self._require_text(reason, "reason")
        timestamp = self.clock()
        with self.store.write() as conn:
            mission = self._require_mission(conn, mission_id)
            if mission.state == "cancelled":
                return mission
            if mission.state not in {"draft", "active", "paused"}:
                raise MissionStateError(f"cannot cancel mission from {mission.state}")
            run_cursor = conn.execute(
                """
                UPDATE task_runs
                SET status = 'cancelled', outcome = 'cancelled', ended_at = ?, error = ?
                WHERE mission_id = ? AND status = 'running'
                """,
                (timestamp, reason, mission_id),
            )
            task_cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                    current_run_id = NULL, version = version + 1, updated_at = ?
                WHERE mission_id = ? AND status IN ('blocked', 'ready', 'retry_wait', 'running')
                """,
                (timestamp, mission_id),
            )
            conn.execute(
                "DELETE FROM resource_leases WHERE task_id IN (SELECT task_id FROM tasks WHERE mission_id = ?)",
                (mission_id,),
            )
            conn.execute(
                "UPDATE missions SET state = 'cancelled', pause_reason = ?, updated_at = ? WHERE mission_id = ?",
                (reason, timestamp, mission_id),
            )
            self.store.append_event(
                conn,
                kind="mission.cancelled",
                actor_id=actor_id,
                actor_type="operator",
                mission_id=mission_id,
                payload={
                    "reason": reason,
                    "cancelled_tasks": task_cursor.rowcount,
                    "cancelled_runs": run_cursor.rowcount,
                },
                created_at=timestamp,
            )
            return self._require_mission(conn, mission_id)

    def create_task(
        self,
        mission_id: str,
        title: str,
        assignee: str,
        *,
        actor_id: str,
        parents: Sequence[str] = (),
        specification: dict[str, Any] | None = None,
        acceptance: dict[str, Any] | None = None,
        priority: int = 0,
        scheduled_at: float | None = None,
        max_runtime_seconds: float = 1_800,
        max_attempts: int = 2,
        idempotency_key: str | None = None,
        resources: Sequence[str] = (),
    ) -> Task:
        title = self._require_text(title, "title")
        assignee = self._require_text(assignee, "assignee")
        actor_id = self._require_text(actor_id, "actor_id")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("priority must be an integer")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if (
            isinstance(max_runtime_seconds, bool)
            or not isinstance(max_runtime_seconds, (int, float))
            or not math.isfinite(float(max_runtime_seconds))
            or max_runtime_seconds <= 0
        ):
            raise ValueError("max_runtime_seconds must be positive and finite")
        normalized_parents = self._string_tuple(parents, "parents")
        normalized_resources = self._string_tuple(resources, "resources")
        specification_json = self._json_object(specification or {}, "specification")
        acceptance_json = self._json_object(acceptance or {}, "acceptance")
        timestamp = self.clock()

        with self.store.write() as conn:
            mission = self._require_mission(conn, mission_id)
            if idempotency_key is not None:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE mission_id = ? AND idempotency_key = ?",
                    (mission_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    task = self._task_from_row(conn, row)
                    event = conn.execute(
                        """
                        SELECT actor_id, payload_json FROM events
                        WHERE kind = 'task.created' AND task_id = ?
                        ORDER BY id LIMIT 1
                        """,
                        (task.task_id,),
                    ).fetchone()
                    creation = {} if event is None else json.loads(event["payload_json"])
                    if (
                        task.title != title
                        or task.assignee != assignee
                        or row["specification_json"] != specification_json
                        or row["acceptance_json"] != acceptance_json
                        or task.priority != priority
                        or task.scheduled_at != scheduled_at
                        or task.max_runtime_seconds != float(max_runtime_seconds)
                        or task.max_attempts != max_attempts
                        or event is None
                        or event["actor_id"] != actor_id
                        or creation.get("parents") != list(normalized_parents)
                        or creation.get("resources") != list(normalized_resources)
                    ):
                        raise WorkflowError(
                            "task idempotency key was reused with a different creation payload"
                        )
                    return task
            if mission.state != "active":
                raise MissionStateError(f"mission is not active: {mission.state}")
            for parent_id in normalized_parents:
                parent = self._require_task(conn, parent_id)
                if parent.mission_id != mission_id:
                    raise WorkflowError("parent task belongs to another mission")
            status = "ready" if self._parents_succeeded(conn, normalized_parents) else "blocked"
            task_id = self._new_id("tsk")
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, mission_id, title, specification_json, acceptance_json,
                    assignee, status, priority, scheduled_at, max_runtime_seconds,
                    max_attempts, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    mission_id,
                    title,
                    specification_json,
                    acceptance_json,
                    assignee,
                    status,
                    priority,
                    scheduled_at,
                    float(max_runtime_seconds),
                    max_attempts,
                    idempotency_key,
                    timestamp,
                    timestamp,
                ),
            )
            conn.executemany(
                "INSERT INTO task_dependencies(parent_task_id, child_task_id, created_at) VALUES (?, ?, ?)",
                [(parent_id, task_id, timestamp) for parent_id in normalized_parents],
            )
            conn.executemany(
                "INSERT INTO task_resources(task_id, resource_key) VALUES (?, ?)",
                [(task_id, resource) for resource in normalized_resources],
            )
            self.store.append_event(
                conn,
                kind="task.created",
                actor_id=actor_id,
                mission_id=mission_id,
                task_id=task_id,
                payload={
                    "assignee": assignee,
                    "status": status,
                    "parents": list(normalized_parents),
                    "resources": list(normalized_resources),
                },
                created_at=timestamp,
            )
            return self._require_task(conn, task_id)

    def get_task(self, task_id: str) -> Task:
        with self.store.read() as conn:
            return self._require_task(conn, task_id)

    def list_tasks(self, mission_id: str | None = None, *, status: str | None = None) -> list[Task]:
        query = "SELECT * FROM tasks WHERE 1 = 1"
        params: list[Any] = []
        if mission_id is not None:
            query += " AND mission_id = ?"
            params.append(mission_id)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY priority DESC, created_at, rowid"
        with self.store.read() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._task_from_row(conn, row) for row in rows]

    def link_dependency(self, parent_task_id: str, child_task_id: str, actor_id: str) -> None:
        if parent_task_id == child_task_id:
            raise DependencyCycleError("dependency would create a cycle")
        timestamp = self.clock()
        with self.store.write() as conn:
            parent = self._require_task(conn, parent_task_id)
            child = self._require_task(conn, child_task_id)
            if parent.mission_id != child.mission_id:
                raise WorkflowError("tasks belong to different missions")
            if child.status in {"running", "succeeded", "failed", "cancelled"}:
                raise WorkflowError(f"cannot add dependency to task in {child.status}")
            if self._path_exists(conn, child_task_id, parent_task_id):
                raise DependencyCycleError("dependency would create a cycle")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO task_dependencies(parent_task_id, child_task_id, created_at)
                VALUES (?, ?, ?)
                """,
                (parent_task_id, child_task_id, timestamp),
            )
            if cursor.rowcount == 0:
                return
            if parent.status != "succeeded":
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', version = version + 1, updated_at = ? WHERE task_id = ?",
                    (timestamp, child_task_id),
                )
            self.store.append_event(
                conn,
                kind="task.dependency_linked",
                actor_id=actor_id,
                mission_id=parent.mission_id,
                task_id=child_task_id,
                payload={"parent_task_id": parent_task_id},
                created_at=timestamp,
            )

    def claim_next(
        self,
        worker_id: str,
        roles: Iterable[str],
        *,
        lease_seconds: float = 900,
    ) -> TaskClaim | None:
        worker_id = self._require_text(worker_id, "worker_id")
        normalized_roles = sorted(set(self._string_tuple(tuple(roles), "roles")))
        if not normalized_roles:
            return None
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be positive and finite")
        timestamp = self.clock()
        placeholders = ",".join("?" for _ in normalized_roles)
        with self.store.write() as conn:
            rows = conn.execute(
                f"""
                SELECT t.* FROM tasks t
                JOIN missions m ON m.mission_id = t.mission_id
                WHERE t.status = 'ready'
                  AND m.state = 'active'
                  AND t.assignee IN ({placeholders})
                  AND t.attempts < t.max_attempts
                  AND (t.scheduled_at IS NULL OR t.scheduled_at <= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM task_dependencies d
                      JOIN tasks p ON p.task_id = d.parent_task_id
                      WHERE d.child_task_id = t.task_id AND p.status != 'succeeded'
                  )
                ORDER BY t.priority DESC, t.created_at, t.rowid
                """,
                [*normalized_roles, timestamp],
            ).fetchall()
            for row in rows:
                task_id = str(row["task_id"])
                resources = self._resources_for(conn, task_id)
                if resources and self._resources_busy(conn, resources, timestamp):
                    continue
                if resources:
                    resource_placeholders = ",".join("?" for _ in resources)
                    conn.execute(
                        f"DELETE FROM resource_leases WHERE resource_key IN ({resource_placeholders}) AND expires_at <= ?",
                        [*resources, timestamp],
                    )
                run_id = self._new_id("run")
                attempt = int(row["attempts"]) + 1
                expires = min(
                    timestamp + float(lease_seconds),
                    timestamp + float(row["max_runtime_seconds"]),
                )
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'running', attempts = ?, lease_owner = ?, lease_expires_at = ?,
                        current_run_id = ?, version = version + 1, updated_at = ?
                    WHERE task_id = ? AND status = 'ready' AND version = ?
                    """,
                    (attempt, worker_id, expires, run_id, timestamp, task_id, int(row["version"])),
                )
                if cursor.rowcount != 1:
                    continue
                conn.execute(
                    """
                    INSERT INTO task_runs(
                        run_id, task_id, mission_id, worker_id, status, attempt,
                        lease_expires_at, last_heartbeat_at, started_at
                    ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
                    """,
                    (run_id, task_id, row["mission_id"], worker_id, attempt, expires, timestamp, timestamp),
                )
                conn.executemany(
                    """
                    INSERT INTO resource_leases(resource_key, task_id, run_id, worker_id, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [(resource, task_id, run_id, worker_id, expires) for resource in resources],
                )
                self.store.append_event(
                    conn,
                    kind="task.claimed",
                    actor_id=worker_id,
                    mission_id=str(row["mission_id"]),
                    task_id=task_id,
                    run_id=run_id,
                    payload={"attempt": attempt, "lease_expires_at": expires, "resources": list(resources)},
                    created_at=timestamp,
                )
                return TaskClaim(self._require_task(conn, task_id), self._require_run(conn, run_id))
        return None

    def heartbeat(
        self,
        task_id: str,
        run_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 900,
    ) -> Task:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be positive and finite")
        timestamp = self.clock()
        with self.store.write() as conn:
            task = self._require_task(conn, task_id)
            self._assert_current_run(task, run_id)
            if task.lease_owner != worker_id:
                raise LeaseError(f"task is owned by another worker: {task.lease_owner}")
            run = self._require_run(conn, run_id)
            runtime_deadline = run.started_at + task.max_runtime_seconds
            if timestamp >= runtime_deadline:
                raise LeaseError("task runtime deadline has passed")
            if task.lease_expires_at is None or task.lease_expires_at <= timestamp:
                raise LeaseError("task lease expired")
            expires = min(timestamp + float(lease_seconds), runtime_deadline)
            conn.execute(
                "UPDATE tasks SET lease_expires_at = ?, version = version + 1, updated_at = ? WHERE task_id = ?",
                (expires, timestamp, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET lease_expires_at = ?, last_heartbeat_at = ? WHERE run_id = ?",
                (expires, timestamp, run_id),
            )
            conn.execute("UPDATE resource_leases SET expires_at = ? WHERE run_id = ?", (expires, run_id))
            self.store.append_event(
                conn,
                kind="run.heartbeat",
                actor_id=worker_id,
                mission_id=task.mission_id,
                task_id=task_id,
                run_id=run_id,
                payload={"lease_expires_at": expires},
                created_at=timestamp,
            )
            return self._require_task(conn, task_id)

    def complete(
        self,
        task_id: str,
        run_id: str,
        worker_id: str,
        summary: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        summary = self._require_text(summary, "summary")
        metadata_json = self._json_object(metadata or {}, "metadata")
        timestamp = self.clock()
        with self.store.write() as conn:
            task = self._require_task(conn, task_id)
            self._assert_current_run(task, run_id)
            if task.lease_owner != worker_id:
                raise LeaseError(f"task is owned by another worker: {task.lease_owner}")
            if task.lease_expires_at is None or task.lease_expires_at <= timestamp:
                raise LeaseError("task lease expired")
            conn.execute(
                """
                UPDATE task_runs
                SET status = 'completed', outcome = 'completed', ended_at = ?,
                    summary = ?, metadata_json = ?, lease_expires_at = NULL
                WHERE run_id = ? AND status = 'running'
                """,
                (timestamp, summary, metadata_json, run_id),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL,
                    current_run_id = NULL, completion_summary = ?, completion_metadata_json = ?,
                    version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (summary, metadata_json, timestamp, task_id),
            )
            conn.execute("DELETE FROM resource_leases WHERE run_id = ?", (run_id,))
            self.store.append_event(
                conn,
                kind="task.completed",
                actor_id=worker_id,
                mission_id=task.mission_id,
                task_id=task_id,
                run_id=run_id,
                payload={"summary": summary, "metadata": json.loads(metadata_json)},
                created_at=timestamp,
            )
            self._promote_children(conn, task_id, timestamp)
            return self._require_task(conn, task_id)

    def fail(
        self,
        task_id: str,
        run_id: str,
        worker_id: str,
        error: str,
        *,
        outcome: str = "failed",
    ) -> Task:
        error = self._require_text(error, "error")
        outcome = self._require_text(outcome, "outcome")
        timestamp = self.clock()
        with self.store.write() as conn:
            task = self._require_task(conn, task_id)
            self._assert_current_run(task, run_id)
            if task.lease_owner != worker_id:
                raise LeaseError(f"task is owned by another worker: {task.lease_owner}")
            run = self._require_run(conn, run_id)
            runtime_deadline = run.started_at + task.max_runtime_seconds
            if timestamp >= runtime_deadline:
                raise LeaseError("task runtime deadline has passed")
            if task.lease_expires_at is None or task.lease_expires_at <= timestamp:
                raise LeaseError("task lease expired")
            next_status = "failed" if task.attempts >= task.max_attempts else "ready"
            conn.execute(
                """
                UPDATE task_runs
                SET status = 'failed', outcome = ?, ended_at = ?, error = ?, lease_expires_at = NULL
                WHERE run_id = ? AND status = 'running'
                """,
                (outcome, timestamp, error, run_id),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    current_run_id = NULL, version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (next_status, timestamp, task_id),
            )
            conn.execute("DELETE FROM resource_leases WHERE run_id = ?", (run_id,))
            self.store.append_event(
                conn,
                kind="run.failed",
                actor_id=worker_id,
                mission_id=task.mission_id,
                task_id=task_id,
                run_id=run_id,
                payload={"outcome": outcome, "error": error, "next_status": next_status},
                created_at=timestamp,
            )
            return self._require_task(conn, task_id)

    def recover_expired(self, actor_id: str) -> list[str]:
        timestamp = self.clock()
        recovered: list[str] = []
        with self.store.write() as conn:
            rows = conn.execute(
                """
                SELECT t.*, r.started_at AS current_run_started_at
                FROM tasks t
                JOIN task_runs r ON r.run_id = t.current_run_id
                WHERE t.status = 'running'
                  AND (
                    (t.lease_expires_at IS NOT NULL AND t.lease_expires_at <= ?)
                    OR r.started_at + t.max_runtime_seconds <= ?
                  )
                ORDER BY t.task_id
                """,
                (timestamp, timestamp),
            ).fetchall()
            for row in rows:
                task = self._task_from_row(conn, row)
                run_id = task.current_run_id
                if run_id is None:
                    raise LeaseError(f"running task has no current run: {task.task_id}")
                next_status = "failed" if task.attempts >= task.max_attempts else "ready"
                runtime_exceeded = (
                    timestamp >= float(row["current_run_started_at"]) + task.max_runtime_seconds
                )
                outcome = "runtime_exceeded" if runtime_exceeded else "expired"
                error = "task runtime exceeded" if runtime_exceeded else "lease expired"
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = 'expired', outcome = ?, ended_at = ?,
                        lease_expires_at = NULL, error = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (outcome, timestamp, error, run_id),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                        current_run_id = NULL, version = version + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (next_status, timestamp, task.task_id),
                )
                conn.execute("DELETE FROM resource_leases WHERE run_id = ?", (run_id,))
                self.store.append_event(
                    conn,
                    kind="run.expired",
                    actor_id=actor_id,
                    actor_type="system",
                    mission_id=task.mission_id,
                    task_id=task.task_id,
                    run_id=run_id,
                    payload={
                        "next_status": next_status,
                        "attempt": task.attempts,
                        "outcome": outcome,
                    },
                    created_at=timestamp,
                )
                recovered.append(task.task_id)
        return recovered

    def list_runs(self, task_id: str) -> list[TaskRun]:
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at, run_id", (task_id,)
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self.store.read() as conn:
            mission_rows = conn.execute(
                "SELECT state, COUNT(*) AS total FROM missions GROUP BY state ORDER BY state"
            ).fetchall()
            task_rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM tasks GROUP BY status ORDER BY status"
            ).fetchall()
            event_row = conn.execute("SELECT COUNT(*) AS total FROM events").fetchone()
            lease_row = conn.execute(
                "SELECT COUNT(*) AS total FROM resource_leases WHERE expires_at > ?",
                (self.clock(),),
            ).fetchone()
        return {
            "missions": {str(row["state"]): int(row["total"]) for row in mission_rows},
            "tasks": {str(row["status"]): int(row["total"]) for row in task_rows},
            "events": 0 if event_row is None else int(event_row["total"]),
            "active_resource_leases": 0 if lease_row is None else int(lease_row["total"]),
        }

    def build_task_context(self, task_id: str) -> dict[str, Any]:
        with self.store.read() as conn:
            task = self._require_task(conn, task_id)
            parent_rows = conn.execute(
                """
                SELECT p.task_id, p.completion_summary, p.completion_metadata_json
                FROM task_dependencies d
                JOIN tasks p ON p.task_id = d.parent_task_id
                WHERE d.child_task_id = ? AND p.status = 'succeeded'
                ORDER BY d.created_at, p.task_id
                """,
                (task_id,),
            ).fetchall()
            prior_rows = conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at, run_id", (task_id,)
            ).fetchall()
        return {
            "task": asdict(task),
            "parent_handoffs": [
                {
                    "task_id": str(row["task_id"]),
                    "summary": str(row["completion_summary"] or ""),
                    "metadata": json.loads(row["completion_metadata_json"]),
                }
                for row in parent_rows
            ],
            "prior_attempts": [asdict(self._run_from_row(row)) for row in prior_rows],
        }

    def _transition_mission(
        self,
        mission_id: str,
        expected: str,
        target: str,
        actor_id: str,
        event_kind: str,
    ) -> Mission:
        timestamp = self.clock()
        with self.store.write() as conn:
            mission = self._require_mission(conn, mission_id)
            if mission.state != expected:
                raise MissionStateError(f"cannot activate mission from {mission.state}")
            conn.execute(
                "UPDATE missions SET state = ?, pause_reason = NULL, updated_at = ? WHERE mission_id = ?",
                (target, timestamp, mission_id),
            )
            self.store.append_event(
                conn,
                kind=event_kind,
                actor_id=actor_id,
                actor_type="operator",
                mission_id=mission_id,
                created_at=timestamp,
            )
            if target == "active":
                self._promote_ready_mission_tasks(conn, mission_id, timestamp)
            return self._require_mission(conn, mission_id)

    def _promote_ready_mission_tasks(
        self,
        conn: sqlite3.Connection,
        mission_id: str,
        timestamp: float,
    ) -> None:
        rows = conn.execute(
            """
            SELECT c.task_id
            FROM tasks c
            WHERE c.mission_id = ? AND c.status = 'blocked'
              AND NOT EXISTS (
                  SELECT 1 FROM task_dependencies d
                  JOIN tasks p ON p.task_id = d.parent_task_id
                  WHERE d.child_task_id = c.task_id AND p.status != 'succeeded'
              )
            ORDER BY c.task_id
            """,
            (mission_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET status = 'ready', version = version + 1, updated_at = ? WHERE task_id = ?",
                (timestamp, row["task_id"]),
            )
            self.store.append_event(
                conn,
                kind="task.promoted",
                actor_id="workflow",
                actor_type="system",
                mission_id=mission_id,
                task_id=str(row["task_id"]),
                created_at=timestamp,
            )

    def _promote_children(self, conn: sqlite3.Connection, parent_task_id: str, timestamp: float) -> None:
        rows = conn.execute(
            """
            SELECT c.task_id, c.mission_id
            FROM task_dependencies d
            JOIN tasks c ON c.task_id = d.child_task_id
            JOIN missions m ON m.mission_id = c.mission_id
            WHERE d.parent_task_id = ? AND c.status = 'blocked' AND m.state = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM task_dependencies d2
                  JOIN tasks p ON p.task_id = d2.parent_task_id
                  WHERE d2.child_task_id = c.task_id AND p.status != 'succeeded'
              )
            ORDER BY c.task_id
            """,
            (parent_task_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET status = 'ready', version = version + 1, updated_at = ? WHERE task_id = ?",
                (timestamp, row["task_id"]),
            )
            self.store.append_event(
                conn,
                kind="task.promoted",
                actor_id="workflow",
                actor_type="system",
                mission_id=str(row["mission_id"]),
                task_id=str(row["task_id"]),
                created_at=timestamp,
            )

    @staticmethod
    def _assert_current_run(task: Task, run_id: str) -> None:
        if task.status != "running" or task.current_run_id != run_id:
            raise LeaseError("run is not the task's current run")

    @staticmethod
    def _parents_succeeded(conn: sqlite3.Connection, parents: Sequence[str]) -> bool:
        if not parents:
            return True
        placeholders = ",".join("?" for _ in parents)
        row = conn.execute(
            f"SELECT COUNT(*) AS total FROM tasks WHERE task_id IN ({placeholders}) AND status = 'succeeded'",
            list(parents),
        ).fetchone()
        return row is not None and int(row["total"]) == len(parents)

    @staticmethod
    def _path_exists(conn: sqlite3.Connection, start: str, target: str) -> bool:
        row = conn.execute(
            """
            WITH RECURSIVE descendants(task_id) AS (
                SELECT child_task_id FROM task_dependencies WHERE parent_task_id = ?
                UNION
                SELECT d.child_task_id
                FROM task_dependencies d
                JOIN descendants x ON d.parent_task_id = x.task_id
            )
            SELECT 1 FROM descendants WHERE task_id = ? LIMIT 1
            """,
            (start, target),
        ).fetchone()
        return row is not None

    @staticmethod
    def _resources_busy(conn: sqlite3.Connection, resources: Sequence[str], timestamp: float) -> bool:
        placeholders = ",".join("?" for _ in resources)
        row = conn.execute(
            f"SELECT 1 FROM resource_leases WHERE resource_key IN ({placeholders}) AND expires_at > ? LIMIT 1",
            [*resources, timestamp],
        ).fetchone()
        return row is not None

    @staticmethod
    def _resources_for(conn: sqlite3.Connection, task_id: str) -> tuple[str, ...]:
        rows = conn.execute(
            "SELECT resource_key FROM task_resources WHERE task_id = ? ORDER BY resource_key", (task_id,)
        ).fetchall()
        return tuple(str(row["resource_key"]) for row in rows)

    def _require_mission(self, conn: sqlite3.Connection, mission_id: str) -> Mission:
        row = conn.execute("SELECT * FROM missions WHERE mission_id = ?", (mission_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"unknown mission: {mission_id}")
        return self._mission_from_row(row)

    def _require_task(self, conn: sqlite3.Connection, task_id: str) -> Task:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"unknown task: {task_id}")
        return self._task_from_row(conn, row)

    def _require_run(self, conn: sqlite3.Connection, run_id: str) -> TaskRun:
        row = conn.execute("SELECT * FROM task_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"unknown run: {run_id}")
        return self._run_from_row(row)

    @staticmethod
    def _mission_from_row(row: sqlite3.Row) -> Mission:
        return Mission(
            mission_id=str(row["mission_id"]),
            goal=str(row["goal"]),
            state=str(row["state"]),
            created_by=str(row["created_by"]),
            limits=json.loads(row["limits_json"]),
            idempotency_key=row["idempotency_key"],
            pause_reason=row["pause_reason"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _task_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Task:
        return Task(
            task_id=str(row["task_id"]),
            mission_id=str(row["mission_id"]),
            title=str(row["title"]),
            specification=json.loads(row["specification_json"]),
            acceptance=json.loads(row["acceptance_json"]),
            assignee=str(row["assignee"]),
            status=str(row["status"]),
            priority=int(row["priority"]),
            scheduled_at=None if row["scheduled_at"] is None else float(row["scheduled_at"]),
            max_runtime_seconds=float(row["max_runtime_seconds"]),
            max_attempts=int(row["max_attempts"]),
            attempts=int(row["attempts"]),
            idempotency_key=row["idempotency_key"],
            resources=self._resources_for(conn, str(row["task_id"])),
            lease_owner=row["lease_owner"],
            lease_expires_at=None if row["lease_expires_at"] is None else float(row["lease_expires_at"]),
            current_run_id=row["current_run_id"],
            completion_summary=row["completion_summary"],
            completion_metadata=json.loads(row["completion_metadata_json"]),
            version=int(row["version"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> TaskRun:
        return TaskRun(
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            mission_id=str(row["mission_id"]),
            worker_id=str(row["worker_id"]),
            status=str(row["status"]),
            outcome=row["outcome"],
            attempt=int(row["attempt"]),
            lease_expires_at=None if row["lease_expires_at"] is None else float(row["lease_expires_at"]),
            last_heartbeat_at=(
                None if row["last_heartbeat_at"] is None else float(row["last_heartbeat_at"])
            ),
            started_at=float(row["started_at"]),
            ended_at=None if row["ended_at"] is None else float(row["ended_at"]),
            summary=row["summary"],
            metadata=json.loads(row["metadata_json"]),
            error=row["error"],
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
