from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .repository_audit import AuditValidationError, repository_digest


def verify_result(request: Mapping[str, Any]) -> dict[str, Any]:
    if request.get("control"):
        raise AuditValidationError("verification request must not contain control credentials")
    specification = request.get("specification")
    if not isinstance(specification, dict) or specification.get("audit_role") != "verifier":
        raise AuditValidationError("verification is allowed only for the verifier task")
    repository_root = specification.get("repository_root")
    expected_digest = specification.get("repository_digest")
    workspace = request.get("workspace")
    if not isinstance(repository_root, str) or not isinstance(expected_digest, str):
        raise AuditValidationError("repository verification contract is incomplete")
    if not isinstance(workspace, str):
        raise AuditValidationError("verification workspace is invalid")
    result_path = Path(workspace).expanduser().resolve() / "verifier-result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditValidationError("verifier result is missing or invalid") from exc
    if (
        not isinstance(result, dict)
        or result.get("role") != "verifier"
        or result.get("verdict") != "pass"
        or result.get("deterministic_evidence_check") != "passed"
    ):
        raise AuditValidationError("verifier did not produce an accepted pass result")
    actual_digest = repository_digest(repository_root)
    if actual_digest != expected_digest:
        raise AuditValidationError("repository changed during the read-only audit")
    return {
        "verified": True,
        "repository_digest": actual_digest,
        "verified_findings": result.get("verified_count"),
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise AuditValidationError("verification request must be an object")
        result = verify_result(request)
    except Exception as exc:
        print(f"repository audit verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
