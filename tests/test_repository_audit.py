from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_loop.repository_audit import (
    AuditValidationError,
    build_evidence_catalog,
    collect_repository_evidence,
    repository_digest,
    resolve_evidence_findings,
    run_audit,
    validate_findings,
)


MODEL = "nemotron-3.5-lightning-30b-a3b"


def test_collect_repository_evidence_is_bounded_and_read_only(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "service.py"
    source.write_text("def authorize():\n    return 'bounded'\n", encoding="utf-8")
    ignored = tmp_path / ".git"
    ignored.mkdir()
    (ignored / "private").write_text("must not enter prompt", encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    evidence = collect_repository_evidence(tmp_path, "security", max_chars=2_000)

    assert "src/service.py" in evidence
    assert "def authorize" in evidence
    assert "must not enter prompt" not in evidence
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_repository_digest_changes_when_admissible_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before = repository_digest(tmp_path)

    source.write_text("VALUE = 2\n", encoding="utf-8")

    assert repository_digest(tmp_path) != before


def test_repository_digest_covers_non_prompt_files_symlinks_and_modes(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")

    before = repository_digest(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: ci\n", encoding="utf-8")
    assert repository_digest(tmp_path) != before

    before = repository_digest(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    assert repository_digest(tmp_path) != before

    before = repository_digest(tmp_path)
    binary = tmp_path / "fixture.bin"
    binary.write_bytes(b"x" * 2_000_001)
    assert repository_digest(tmp_path) != before

    before = repository_digest(tmp_path)
    link = tmp_path / "current-config"
    link.symlink_to("Dockerfile")
    assert repository_digest(tmp_path) != before

    before = repository_digest(tmp_path)
    link.unlink()
    link.symlink_to("service.py")
    assert repository_digest(tmp_path) != before

    before = repository_digest(tmp_path)
    source.chmod(0o755)
    assert repository_digest(tmp_path) != before

    before = repository_digest(tmp_path)
    runtime = tmp_path / ".agent-loop" / "control.db"
    runtime.parent.mkdir()
    runtime.write_bytes(b"mutable runtime state")
    assert repository_digest(tmp_path) == before


def test_repository_digest_uses_unambiguous_length_framing(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a").write_bytes(b"X\0file\0b\000644\0Y")
    (two / "a").write_bytes(b"X")
    (two / "b").write_bytes(b"Y")

    assert repository_digest(one) != repository_digest(two)


def test_repository_digest_fails_closed_when_directory_cannot_be_enumerated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "visible.py").write_text("VISIBLE = True\n", encoding="utf-8")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "hidden.py").write_text("HIDDEN = True\n", encoding="utf-8")
    real_scandir = os.scandir

    def guarded_scandir(path: str | os.PathLike[str]):
        if Path(path) == blocked:
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    with pytest.raises(AuditValidationError, match="directory is unreadable: blocked"):
        repository_digest(tmp_path)


def test_findings_require_safe_existing_file_and_exact_evidence(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text("ALLOWED = {'read'}\n", encoding="utf-8")
    valid = [
        {
            "severity": "moderate",
            "file": "service.py",
            "evidence": "ALLOWED = {'read'}",
            "finding": "The allowlist is explicit.",
            "recommendation": "Keep the allowlist narrow.",
        }
    ]

    assert validate_findings(tmp_path, valid) == valid
    with pytest.raises(AuditValidationError, match="outside"):
        validate_findings(tmp_path, [{**valid[0], "file": "../secret"}])
    with pytest.raises(AuditValidationError, match="exact substring"):
        validate_findings(tmp_path, [{**valid[0], "evidence": "invented evidence"}])


def test_evidence_catalog_binds_ids_to_exact_repository_substrings(tmp_path: Path) -> None:
    (tmp_path / "SECURITY.md").write_text("Workers have no database access.\n", encoding="utf-8")

    rendered, catalog = build_evidence_catalog(tmp_path, "security", max_chars=2_000)

    assert rendered == "[E0001] file=SECURITY.md :: Workers have no database access."
    assert catalog == {"E0001": ("SECURITY.md", "Workers have no database access.")}


def test_evidence_catalog_rendering_respects_final_character_limit(tmp_path: Path) -> None:
    (tmp_path / "SECURITY.md").write_text(
        "\n".join(f"evidence line {index}" for index in range(500)),
        encoding="utf-8",
    )

    rendered, catalog = build_evidence_catalog(tmp_path, "security", max_chars=512)

    assert len(rendered) <= 512
    assert catalog


def test_unknown_evidence_id_is_rejected_before_canonicalization(tmp_path: Path) -> None:
    (tmp_path / "SECURITY.md").write_text("Workers have no database access.\n", encoding="utf-8")

    with pytest.raises(AuditValidationError, match="unknown evidence ID"):
        resolve_evidence_findings(
            tmp_path,
            {"E0001": ("SECURITY.md", "Workers have no database access.")},
            [
                {
                    "severity": "high",
                    "evidence_id": "E9999",
                    "finding": "Unsupported finding",
                    "recommendation": "Reject it",
                }
            ],
        )


def test_security_worker_calls_broker_and_publishes_verified_artifact(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "SECURITY.md").write_text("Workers have no database access.\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker_requests: list[dict[str, object]] = []
    control_requests: list[tuple[str, dict[str, object]]] = []

    def fake_broker(_socket: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        broker_requests.append(payload)
        return {
            "model": MODEL,
            "content": json.dumps(
                {
                    "summary": "One verified boundary.",
                    "findings": [
                        {
                            "severity": "moderate",
                            "evidence_id": "E0001",
                            "finding": "The database boundary is documented.",
                            "recommendation": "Retain the boundary test.",
                        }
                    ],
                }
            ),
            "finish_reason": "stop",
            "usage": {"total_tokens": 50},
        }

    def fake_control(
        _socket: str,
        _token: str,
        method: str,
        params: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        values = params or {}
        control_requests.append((method, values))
        if method == "message.list":
            return []
        if method == "message.publish":
            return {"message_id": "message-1"}
        if method == "artifact.put":
            content = base64.b64decode(str(values["content_base64"]), validate=True)
            assert b"Workers have no database access." in content
            return {"artifact_id": "artifact-1"}
        if method == "heartbeat":
            return {"status": "running"}
        raise AssertionError(method)

    request = {
        "mission_id": "mission-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "worker_id": "security-1",
        "goal": "Audit the repository",
        "specification": {
            "audit_role": "security",
            "repository_root": str(repository),
            "model_broker_socket": str(tmp_path / "model.sock"),
            "model_request": {
                "model": MODEL,
                "max_tokens": 512,
                "max_prompt_chars": 8_000,
                "temperature": 0,
            },
        },
        "acceptance": {},
        "context": {},
        "workspace": str(workspace),
        "limits": {},
        "control": {"socket_path": str(tmp_path / "control.sock"), "token": "run-token"},
    }

    result = run_audit(request, broker=fake_broker, control=fake_control)

    assert result["outcome"] == "candidate_complete"
    assert result["artifact_ids"] == ["artifact-1"]
    assert broker_requests[0]["run_token"] == "run-token"
    assert [method for method, _ in control_requests].count("heartbeat") == 2
    assert any(method == "message.publish" for method, _ in control_requests)
    published = next(values for method, values in control_requests if method == "message.publish")
    assert published["kind"] == "review_note"
    assert any(method == "artifact.put" for method, _ in control_requests)
    assert (workspace / "security-result.json").is_file()
