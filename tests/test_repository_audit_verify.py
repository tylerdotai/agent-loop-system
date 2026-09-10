from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_loop.repository_audit import AuditValidationError, repository_digest
from agent_loop.repository_audit_verify import verify_result


def request(repository: Path, workspace: Path, digest: str) -> dict[str, object]:
    return {
        "mission_id": "mission-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "worker_id": "verifier-1",
        "specification": {
            "audit_role": "verifier",
            "repository_root": str(repository),
            "repository_digest": digest,
        },
        "workspace": str(workspace),
        "control": {},
    }


def test_verifier_requires_model_pass_and_unchanged_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "service.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    digest = repository_digest(repository)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "verifier-result.json").write_text(
        json.dumps(
            {
                "role": "verifier",
                "verdict": "pass",
                "verified_count": 2,
                "deterministic_evidence_check": "passed",
            }
        ),
        encoding="utf-8",
    )

    result = verify_result(request(repository, workspace, digest))

    assert result == {
        "verified": True,
        "repository_digest": digest,
        "verified_findings": 2,
    }
    source.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(AuditValidationError, match="changed"):
        verify_result(request(repository, workspace, digest))


def test_verifier_rejects_control_credentials_and_model_failure(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    digest = repository_digest(repository)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "verifier-result.json").write_text(
        json.dumps(
            {
                "role": "verifier",
                "verdict": "fail",
                "verified_count": 0,
                "deterministic_evidence_check": "passed",
            }
        ),
        encoding="utf-8",
    )
    value = request(repository, workspace, digest)

    with pytest.raises(AuditValidationError, match="accepted pass"):
        verify_result(value)
    value["control"] = {"token": "must-not-cross"}
    with pytest.raises(AuditValidationError, match="must not contain"):
        verify_result(value)
