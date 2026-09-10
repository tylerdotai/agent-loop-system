from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_loop.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactValidationError,
)
from agent_loop.persistence import SQLiteStore


def make_store(tmp_path: Path, *, max_bytes: int = 1024) -> ArtifactStore:
    return ArtifactStore(SQLiteStore(tmp_path / "control.db"), tmp_path / "artifacts", max_bytes=max_bytes)


def test_content_addressed_artifact_is_durable_and_hash_verified(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    content = b"verified report\n"

    artifact = store.put_bytes(
        mission_id="mission-1",
        task_id="task-1",
        actor_id="writer-1",
        filename="report.md",
        content=content,
        media_type="text/markdown",
    )

    assert artifact.sha256 == hashlib.sha256(content).hexdigest()
    assert Path(artifact.storage_path).is_relative_to(tmp_path / "artifacts")
    assert store.read_bytes(artifact.artifact_id) == content
    assert store.get(artifact.artifact_id) == artifact

    reopened = ArtifactStore(SQLiteStore(tmp_path / "control.db"), tmp_path / "artifacts")
    assert reopened.read_bytes(artifact.artifact_id) == content


def test_dedupe_key_reuses_identical_artifact_and_rejects_changed_content(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.put_bytes(
        "mission-1",
        "task-1",
        "writer-1",
        "report.md",
        b"one",
        dedupe_key="task-1:report:v1",
    )
    duplicate = store.put_bytes(
        "mission-1",
        "task-1",
        "writer-1",
        "report.md",
        b"one",
        dedupe_key="task-1:report:v1",
    )

    assert duplicate == first

    with pytest.raises(ArtifactValidationError, match="idempotency"):
        store.put_bytes(
            "mission-1",
            "task-2",
            "writer-1",
            "retry-name.md",
            b"one",
            dedupe_key="task-1:report:v1",
        )

    with pytest.raises(ArtifactValidationError, match="different content"):
        store.put_bytes(
            "mission-1",
            "task-1",
            "writer-1",
            "report.md",
            b"two",
            dedupe_key="task-1:report:v1",
        )


def test_artifact_filename_rejects_path_traversal_and_content_limit(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_bytes=4)

    with pytest.raises(ArtifactValidationError, match="plain basename"):
        store.put_bytes("mission-1", "task-1", "writer-1", "../secret", b"safe")

    with pytest.raises(ArtifactValidationError, match="maximum size"):
        store.put_bytes("mission-1", "task-1", "writer-1", "large.bin", b"12345")


def test_artifact_read_detects_blob_tampering(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = store.put_bytes("mission-1", "task-1", "writer-1", "result.txt", b"original")
    Path(artifact.storage_path).write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
        store.read_bytes(artifact.artifact_id)


def test_put_file_uses_source_basename_and_records_event(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    source = tmp_path / "candidate.json"
    source.write_text('{"ok":true}\n')

    artifact = store.put_file("mission-1", "task-1", "worker-1", source)

    assert artifact.filename == "candidate.json"
    assert artifact.size_bytes == source.stat().st_size
    events = store.store.list_events(kind="artifact.stored")
    assert len(events) == 1
    assert events[0].payload["artifact_id"] == artifact.artifact_id


def test_artifact_rejects_symlinked_digest_directory(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifacts = ArtifactStore(SQLiteStore(tmp_path / "control.db"), root)
    content = b"cannot escape"
    digest = hashlib.sha256(content).hexdigest()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / digest[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactIntegrityError, match="symbolic link|escaped"):
        artifacts.put_bytes("mission-1", "task-1", "worker", "proof.txt", content)

    assert not (outside / digest).exists()


def test_artifact_idempotent_retry_revalidates_existing_blob(tmp_path: Path) -> None:
    artifacts = ArtifactStore(SQLiteStore(tmp_path / "control.db"), tmp_path / "artifacts")
    artifact = artifacts.put_bytes(
        "mission-1",
        "task-1",
        "worker",
        "proof.txt",
        b"durable proof",
        dedupe_key="proof:v1",
    )
    Path(artifact.storage_path).unlink()

    with pytest.raises(ArtifactIntegrityError, match="missing"):
        artifacts.put_bytes(
            "mission-1",
            "task-1",
            "worker",
            "proof.txt",
            b"durable proof",
            dedupe_key="proof:v1",
        )
