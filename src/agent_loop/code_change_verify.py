from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from .code_change import (
    ChangeValidationError,
    PreparedCodeChange,
    capture_code_change_patch,
)


def verify_code_change_result(request: Mapping[str, Any]) -> dict[str, Any]:
    if request.get("control"):
        raise ChangeValidationError(
            "verification request must not contain control credentials"
        )
    specification = request.get("specification")
    if (
        not isinstance(specification, Mapping)
        or specification.get("change_role") != "verifier"
    ):
        raise ChangeValidationError(
            "verification is allowed only for the verifier task"
        )
    prepared = PreparedCodeChange(
        repository_root=_required_text(
            specification.get("repository_root"), "repository_root"
        ),
        worktree_root=_required_text(
            specification.get("worktree_root"), "worktree_root"
        ),
        base_commit=_required_text(specification.get("base_commit"), "base_commit"),
        source_digest=_required_text(
            specification.get("source_digest"), "source_digest"
        ),
        worktree_gitfile_sha256=_required_text(
            specification.get("worktree_gitfile_sha256"), "worktree_gitfile_sha256"
        ),
    )
    result_root = (
        Path(_required_text(specification.get("result_root"), "result_root"))
        .expanduser()
        .resolve()
    )
    result_path = (
        Path(
            _required_text(
                specification.get("verification_result_path"),
                "verification_result_path",
            )
        )
        .expanduser()
        .resolve()
    )
    if (
        not result_root.is_dir()
        or result_root.is_symlink()
        or not result_path.is_relative_to(result_root)
    ):
        raise ChangeValidationError("verification result path is invalid")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChangeValidationError("verifier result is missing or invalid") from exc
    if (
        not isinstance(result, dict)
        or result.get("change_role") != "verifier"
        or result.get("verdict") != "pass"
    ):
        raise ChangeValidationError("verifier did not produce an accepted pass result")
    changed_paths = result.get("changed_paths")
    if (
        not isinstance(changed_paths, list)
        or not changed_paths
        or not all(isinstance(path, str) and path for path in changed_paths)
    ):
        raise ChangeValidationError("verifier changed path list is invalid")
    max_patch_bytes = specification.get("max_patch_bytes")
    if (
        isinstance(max_patch_bytes, bool)
        or not isinstance(max_patch_bytes, int)
        or max_patch_bytes < 1
    ):
        raise ChangeValidationError("max_patch_bytes must be positive")
    captured = capture_code_change_patch(
        prepared,
        changed_paths,
        max_patch_bytes=max_patch_bytes,
    )
    patch_path = (
        Path(_required_text(result.get("patch_path"), "patch_path"))
        .expanduser()
        .resolve()
    )
    if (
        not patch_path.is_relative_to(result_root)
        or not patch_path.is_file()
        or patch_path.is_symlink()
    ):
        raise ChangeValidationError("verified patch file is invalid")
    patch = patch_path.read_bytes()
    expected_sha = _required_text(result.get("patch_sha256"), "patch_sha256")
    if patch != captured.content or hashlib.sha256(patch).hexdigest() != expected_sha:
        raise ChangeValidationError("verified patch does not match the worktree")
    apply_check = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={prepared.repository_root}",
            "-C",
            prepared.repository_root,
            "apply",
            "--check",
            "--binary",
            "-",
        ],
        input=patch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if apply_check.returncode != 0:
        detail = apply_check.stderr.decode("utf-8", errors="replace").strip()
        raise ChangeValidationError(
            f"patch does not apply to the source checkout: {detail}"
        )
    return {
        "verified": True,
        "base_commit": prepared.base_commit,
        "patch_sha256": captured.sha256,
        "changed_paths": list(captured.changed_paths),
    }


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64_000:
        raise ChangeValidationError(f"{name} must be a non-empty bounded string")
    return value.strip()


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ChangeValidationError("verification request must be an object")
        result = verify_code_change_result(request)
    except Exception as exc:
        print(
            f"code-change verification failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
