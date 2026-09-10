from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .persistence import SQLiteStore


class ArtifactValidationError(ValueError):
    """Raised before invalid artifact metadata or content is stored."""


class ArtifactIntegrityError(RuntimeError):
    """Raised when stored bytes no longer match the recorded content hash."""


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    mission_id: str
    task_id: str
    actor_id: str
    filename: str
    media_type: str | None
    sha256: str
    size_bytes: int
    storage_path: str
    dedupe_key: str | None
    created_at: float


_ARTIFACT_SCHEMA = """
INSERT OR IGNORE INTO schema_meta(component, version) VALUES ('artifacts', 1);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    dedupe_key TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_mission_dedupe
    ON artifacts(mission_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_task_created
    ON artifacts(task_id, created_at, artifact_id);
"""


class ArtifactStore:
    """Content-addressed local artifact storage with durable provenance."""

    def __init__(
        self,
        store: SQLiteStore,
        root: str | Path,
        *,
        max_bytes: int = 25 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.store = store
        self.root = Path(root).expanduser().resolve()
        self.max_bytes = max_bytes
        self.clock = clock
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        with self.store.connect() as conn:
            conn.executescript(_ARTIFACT_SCHEMA)

    def put_bytes(
        self,
        mission_id: str,
        task_id: str,
        actor_id: str,
        filename: str,
        content: bytes,
        *,
        media_type: str | None = None,
        dedupe_key: str | None = None,
    ) -> Artifact:
        mission_id = self._require_text(mission_id, "mission_id")
        task_id = self._require_text(task_id, "task_id")
        actor_id = self._require_text(actor_id, "actor_id")
        filename = self._validate_filename(filename)
        if not isinstance(content, bytes):
            raise ArtifactValidationError("artifact content must be bytes")
        if len(content) > self.max_bytes:
            raise ArtifactValidationError(
                f"artifact exceeds maximum size of {self.max_bytes} bytes"
            )
        if media_type is not None:
            media_type = self._require_text(media_type, "media_type")
        if dedupe_key is not None:
            dedupe_key = self._require_text(dedupe_key, "dedupe_key")
        digest = hashlib.sha256(content).hexdigest()
        timestamp = self.clock()

        with self.store.write() as conn:
            if dedupe_key is not None:
                existing = conn.execute(
                    "SELECT * FROM artifacts WHERE mission_id = ? AND dedupe_key = ?",
                    (mission_id, dedupe_key),
                ).fetchone()
                if existing is not None:
                    artifact = self._artifact_from_row(existing)
                    if artifact.sha256 != digest:
                        raise ArtifactValidationError(
                            "artifact dedupe key was reused with different content"
                        )
                    if (
                        artifact.task_id != task_id
                        or artifact.actor_id != actor_id
                        or artifact.filename != filename
                        or artifact.media_type != media_type
                    ):
                        raise ArtifactValidationError(
                            "artifact idempotency key was reused with different metadata"
                        )
                    self._read_verified(artifact)
                    return artifact

            storage_path = self.root / digest[:2] / digest
            self._ensure_digest_directory(storage_path.parent)
            os.chmod(storage_path.parent, 0o700)
            if storage_path.exists():
                if storage_path.is_symlink():
                    raise ArtifactIntegrityError(
                        f"content-addressed blob must not be a symbolic link: {storage_path}"
                    )
                os.chmod(storage_path, 0o600)
                stored_digest = hashlib.sha256(storage_path.read_bytes()).hexdigest()
                if stored_digest != digest:
                    raise ArtifactIntegrityError(
                        f"existing content-addressed blob hash mismatch: {storage_path}"
                    )
            else:
                temporary = storage_path.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, storage_path)
                except Exception:
                    if temporary.exists():
                        temporary.unlink()
                    raise

            artifact_id = self._new_id("art")
            conn.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, mission_id, task_id, actor_id, filename, media_type,
                    sha256, size_bytes, storage_path, dedupe_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    mission_id,
                    task_id,
                    actor_id,
                    filename,
                    media_type,
                    digest,
                    len(content),
                    str(storage_path),
                    dedupe_key,
                    timestamp,
                ),
            )
            self.store.append_event(
                conn,
                kind="artifact.stored",
                actor_id=actor_id,
                mission_id=mission_id,
                task_id=task_id,
                payload={
                    "artifact_id": artifact_id,
                    "filename": filename,
                    "sha256": digest,
                    "size_bytes": len(content),
                },
                created_at=timestamp,
            )
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("artifact could not be read back")
            return self._artifact_from_row(row)

    def put_file(
        self,
        mission_id: str,
        task_id: str,
        actor_id: str,
        source: str | Path,
        *,
        media_type: str | None = None,
        dedupe_key: str | None = None,
    ) -> Artifact:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ArtifactValidationError(f"artifact source is not a file: {path}")
        size = path.stat().st_size
        if size > self.max_bytes:
            raise ArtifactValidationError(
                f"artifact exceeds maximum size of {self.max_bytes} bytes"
            )
        return self.put_bytes(
            mission_id,
            task_id,
            actor_id,
            path.name,
            path.read_bytes(),
            media_type=media_type,
            dedupe_key=dedupe_key,
        )

    def get(self, artifact_id: str) -> Artifact:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise ArtifactValidationError(f"unknown artifact: {artifact_id}")
        return self._artifact_from_row(row)

    def list_for_task(self, task_id: str) -> list[Artifact]:
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at, artifact_id",
                (task_id,),
            ).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.get(artifact_id)
        return self._read_verified(artifact)

    def _read_verified(self, artifact: Artifact) -> bytes:
        path = Path(artifact.storage_path)
        if not path.exists():
            raise ArtifactIntegrityError(f"artifact blob is missing: {artifact.artifact_id}")
        if path.is_symlink():
            raise ArtifactIntegrityError("artifact blob must not be a symbolic link")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise ArtifactIntegrityError("artifact storage path escaped the configured root") from exc
        if resolved != path or path.parent.resolve(strict=True) != path.parent:
            raise ArtifactIntegrityError("artifact storage path contains a symbolic link")
        if not resolved.is_file():
            raise ArtifactIntegrityError(f"artifact blob is missing: {artifact.artifact_id}")
        content = resolved.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != artifact.sha256:
            raise ArtifactIntegrityError(f"artifact hash mismatch: {artifact.artifact_id}")
        if len(content) != artifact.size_bytes:
            raise ArtifactIntegrityError(f"artifact size mismatch: {artifact.artifact_id}")
        return content

    def _ensure_digest_directory(self, directory: Path) -> None:
        if self.root.is_symlink() or not self.root.is_dir() or self.root.resolve() != self.root:
            raise ArtifactIntegrityError("artifact root must be a real directory")
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise ArtifactIntegrityError("artifact digest directory must not be a symbolic link")
        else:
            directory.mkdir(mode=0o700)
        try:
            resolved = directory.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise ArtifactIntegrityError("artifact digest directory escaped the configured root") from exc
        if resolved != directory:
            raise ArtifactIntegrityError("artifact digest directory must not be a symbolic link")

    @staticmethod
    def _validate_filename(filename: str) -> str:
        filename = ArtifactStore._require_text(filename, "filename")
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise ArtifactValidationError("artifact filename must be a plain basename")
        if filename in {".", ".."}:
            raise ArtifactValidationError("artifact filename must be a plain basename")
        return filename

    @staticmethod
    def _require_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ArtifactValidationError(f"{field_name} must not be empty")
        return value.strip()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=str(row["artifact_id"]),
            mission_id=str(row["mission_id"]),
            task_id=str(row["task_id"]),
            actor_id=str(row["actor_id"]),
            filename=str(row["filename"]),
            media_type=row["media_type"],
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
            storage_path=str(row["storage_path"]),
            dedupe_key=row["dedupe_key"],
            created_at=float(row["created_at"]),
        )
