from __future__ import annotations

import base64
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .model_broker import broker_call
from .repository_audit import collect_repository_evidence, repository_digest
from .workflow import Mission, Task, WorkflowService
from .worker_api import control_call


class ChangeValidationError(ValueError):
    """Raised when a code-change mission or patch violates its contract."""


@dataclass(frozen=True)
class PreparedCodeChange:
    repository_root: str
    worktree_root: str
    base_commit: str
    source_digest: str
    worktree_gitfile_sha256: str


@dataclass(frozen=True)
class EditReceipt:
    path: str
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class CodeChangePatch:
    content: bytes
    sha256: str
    changed_paths: tuple[str, ...]
    base_commit: str


@dataclass(frozen=True)
class CodeChangeMission:
    mission: Mission
    tasks: dict[str, Task]
    prepared: PreparedCodeChange
    verification_result_path: str


_MISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ROLES = ("planner", "implementer", "reviewer", "verifier")


def prepare_code_change_worktree(
    repository_root: str | Path,
    worktree_parent: str | Path,
    mission_id: str,
) -> PreparedCodeChange:
    if not isinstance(mission_id, str) or not _MISSION_ID.fullmatch(mission_id):
        raise ChangeValidationError("mission identifier is invalid")
    repository_input = Path(repository_root).expanduser()
    parent_input = Path(worktree_parent).expanduser()
    if repository_input.is_symlink() or not repository_input.is_dir():
        raise ChangeValidationError("source repository must be a real directory")
    if parent_input.is_symlink() or not parent_input.is_dir():
        raise ChangeValidationError("worktree parent must be a real directory")
    repository = repository_input.resolve()
    parent = parent_input.resolve()
    top_level = _git_text(
        repository,
        "rev-parse",
        "--show-toplevel",
        error="source is not a Git repository",
    )
    if Path(top_level).resolve() != repository:
        raise ChangeValidationError("source repository must be the Git top level")
    if _git_bytes(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ChangeValidationError("source repository must be clean")
    base_commit = _git_text(
        repository, "rev-parse", "HEAD", error="source HEAD could not be resolved"
    )
    source_digest = repository_digest(repository)
    worktree = (parent / mission_id).resolve()
    if worktree.parent != parent or worktree.exists():
        raise ChangeValidationError(
            "worktree target must be a new direct child of the worktree parent"
        )
    completed = _run_git(
        repository,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "submodule.recurse=false",
        "worktree",
        "add",
        "--detach",
        str(worktree),
        base_commit,
    )
    if completed.returncode != 0:
        raise ChangeValidationError(
            _git_error(completed, "Git worktree creation failed")
        )
    try:
        gitfile = worktree / ".git"
        if (
            not worktree.is_dir()
            or worktree.is_symlink()
            or not gitfile.is_file()
            or gitfile.is_symlink()
        ):
            raise ChangeValidationError("Git did not create a valid linked worktree")
        if (
            _git_text(
                worktree,
                "rev-parse",
                "HEAD",
                error="worktree HEAD could not be resolved",
            )
            != base_commit
        ):
            raise ChangeValidationError(
                "worktree HEAD does not match the pinned base commit"
            )
        if repository_digest(repository) != source_digest:
            raise ChangeValidationError(
                "source checkout changed while preparing the worktree"
            )
        gitfile_sha256 = hashlib.sha256(gitfile.read_bytes()).hexdigest()
    except Exception:
        _remove_worktree(repository, worktree)
        raise
    return PreparedCodeChange(
        repository_root=str(repository),
        worktree_root=str(worktree),
        base_commit=base_commit,
        source_digest=source_digest,
        worktree_gitfile_sha256=gitfile_sha256,
    )


def remove_code_change_worktree(prepared: PreparedCodeChange) -> None:
    repository = Path(prepared.repository_root).expanduser().resolve()
    worktree = Path(prepared.worktree_root).expanduser().resolve()
    if worktree.exists():
        _remove_worktree(repository, worktree)


def apply_model_edits(
    worktree_root: str | Path,
    planned_paths: Sequence[str],
    edits: Any,
    *,
    max_total_bytes: int,
) -> tuple[EditReceipt, ...]:
    worktree = _real_directory(worktree_root, "worktree")
    if (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or max_total_bytes < 1
    ):
        raise ChangeValidationError("edit byte limit must be positive")
    approved = {_relative_path(value) for value in planned_paths}
    if not approved:
        raise ChangeValidationError("the planner did not approve any paths")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise ChangeValidationError(
            "model edits must contain between one and eight entries"
        )
    prepared: list[tuple[str, Path, bytes, bytes, int]] = []
    seen: set[str] = set()
    total = 0
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {
            "path",
            "expected_sha256",
            "content",
        }:
            raise ChangeValidationError(
                "each model edit must contain path, expected_sha256, and content"
            )
        relative = _relative_path(edit.get("path"))
        if relative not in approved:
            raise ChangeValidationError("model edit path is not a planned path")
        if relative in seen:
            raise ChangeValidationError("model edit paths must be unique")
        seen.add(relative)
        path = (worktree / relative).resolve()
        if not path.is_relative_to(worktree) or not path.is_file() or path.is_symlink():
            raise ChangeValidationError("model edits require a real tracked text file")
        tracked = _run_git(worktree, "ls-files", "--error-unmatch", "--", relative)
        if tracked.returncode != 0:
            raise ChangeValidationError("model edits require a real tracked text file")
        before = path.read_bytes()
        if b"\x00" in before:
            raise ChangeValidationError("model edits require UTF-8 text")
        try:
            before.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChangeValidationError("model edits require UTF-8 text") from exc
        expected = edit.get("expected_sha256")
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise ChangeValidationError("expected file hash is invalid")
        before_hash = hashlib.sha256(before).hexdigest()
        if before_hash != expected:
            raise ChangeValidationError("planned file hash no longer matches")
        content = edit.get("content")
        if not isinstance(content, str) or "\x00" in content:
            raise ChangeValidationError("replacement content must be UTF-8 text")
        after = content.encode("utf-8")
        total += len(after)
        if total > max_total_bytes:
            raise ChangeValidationError("replacement content exceeds the byte limit")
        if after == before:
            raise ChangeValidationError("model edit does not change the file")
        prepared.append((relative, path, before, after, path.stat().st_mode))
    written: list[tuple[Path, bytes, int]] = []
    try:
        for _relative, path, before, after, mode in prepared:
            _atomic_write(path, after, mode)
            written.append((path, before, mode))
    except Exception:
        for path, before, mode in reversed(written):
            _atomic_write(path, before, mode)
        raise
    return tuple(
        EditReceipt(
            path=relative,
            before_sha256=hashlib.sha256(before).hexdigest(),
            after_sha256=hashlib.sha256(after).hexdigest(),
        )
        for relative, _path, before, after, _mode in prepared
    )


def capture_code_change_patch(
    prepared: PreparedCodeChange,
    planned_paths: Sequence[str],
    *,
    max_patch_bytes: int,
) -> CodeChangePatch:
    if (
        isinstance(max_patch_bytes, bool)
        or not isinstance(max_patch_bytes, int)
        or max_patch_bytes < 1
    ):
        raise ChangeValidationError("patch byte limit must be positive")
    repository = _real_directory(prepared.repository_root, "source repository")
    worktree = _real_directory(prepared.worktree_root, "worktree")
    approved = {_relative_path(value) for value in planned_paths}
    if repository_digest(repository) != prepared.source_digest:
        raise ChangeValidationError("source checkout changed during the mission")
    if (
        _git_text(
            worktree, "rev-parse", "HEAD", error="worktree HEAD could not be resolved"
        )
        != prepared.base_commit
    ):
        raise ChangeValidationError("worktree HEAD changed during the mission")
    gitfile = worktree / ".git"
    if (
        hashlib.sha256(gitfile.read_bytes()).hexdigest()
        != prepared.worktree_gitfile_sha256
    ):
        raise ChangeValidationError("worktree Git link changed during the mission")
    if _run_git(worktree, "diff", "--cached", "--quiet", "--").returncode != 0:
        raise ChangeValidationError("worktree index must remain unchanged")
    untracked = _git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked:
        raise ChangeValidationError(
            "code-change missions support tracked file modifications only"
        )
    changed_raw = _git_bytes(worktree, "diff", "--name-only", "-z", "--")
    changed_paths = tuple(
        part.decode("utf-8") for part in changed_raw.split(b"\0") if part
    )
    if not changed_paths:
        raise ChangeValidationError("the implementer did not produce a patch")
    if any(path not in approved for path in changed_paths):
        raise ChangeValidationError(
            "worktree contains a change outside the approved plan"
        )
    check = _run_git(
        worktree,
        "-c",
        "core.whitespace=cr-at-eol",
        "diff",
        "--check",
        "--",
        *changed_paths,
    )
    if check.returncode != 0:
        raise ChangeValidationError(
            _git_error(check, "patch failed Git whitespace validation")
        )
    patch = _git_bytes(
        worktree,
        "diff",
        "--no-ext-diff",
        "--binary",
        "--full-index",
        "--no-color",
        "--",
        *changed_paths,
    )
    if len(patch) > max_patch_bytes:
        raise ChangeValidationError("patch exceeds the configured byte limit")
    return CodeChangePatch(
        content=patch,
        sha256=hashlib.sha256(patch).hexdigest(),
        changed_paths=changed_paths,
        base_commit=prepared.base_commit,
    )


def create_code_change_mission(
    workflow: WorkflowService,
    *,
    goal: str,
    actor_id: str,
    repository_root: str | Path,
    worktree_parent: str | Path,
    result_root: str | Path,
    worker_command: Sequence[str],
    verifier_command: Sequence[str],
    model_broker_socket: str | Path,
    model_request: Mapping[str, Any],
    max_patch_bytes: int = 24_000,
    worker_user: str | None = None,
) -> CodeChangeMission:
    worker = _command(worker_command, "worker command")
    verifier = _command(verifier_command, "verifier command")
    if not isinstance(model_request, Mapping):
        raise ChangeValidationError("model request must be an object")
    model = dict(model_request)
    for field in ("model", "max_tokens", "max_prompt_chars", "temperature"):
        if field not in model:
            raise ChangeValidationError(f"model request is missing {field}")
    results = _real_directory(result_root, "result root")
    mission = workflow.create_mission(
        goal,
        actor_id,
        state="active",
        limits={"max_patch_bytes": max_patch_bytes},
    )
    prepared: PreparedCodeChange | None = None
    try:
        prepared = prepare_code_change_worktree(
            repository_root, worktree_parent, mission.mission_id
        )
        mission_results = results / mission.mission_id
        mission_results.mkdir(mode=0o700)
        _handoff_worktree(prepared, mission_results, worker_user)
        verification_result = mission_results / "verifier-result.json"
        common = {
            "command": list(worker),
            "cwd": prepared.worktree_root,
            "repository_root": prepared.repository_root,
            "worktree_root": prepared.worktree_root,
            "base_commit": prepared.base_commit,
            "source_digest": prepared.source_digest,
            "worktree_gitfile_sha256": prepared.worktree_gitfile_sha256,
            "model_broker_socket": str(
                Path(model_broker_socket).expanduser().resolve()
            ),
            "model_request": model,
            "max_patch_bytes": max_patch_bytes,
            "result_root": str(results),
            "verification_result_path": str(verification_result),
        }
        tasks: dict[str, Task] = {}
        parent: str | None = None
        for role in _ROLES:
            specification = {**common, "change_role": role}
            acceptance: dict[str, Any] = {
                "required_evidence_kinds": ["model_request"],
                "minimum_artifacts": 1,
            }
            if role in {"implementer", "verifier"}:
                acceptance["required_evidence_kinds"].append("patch")
                acceptance["minimum_artifacts"] = 2
            if role == "verifier":
                acceptance["verification_command"] = list(verifier)
                acceptance["verification_timeout_seconds"] = 120
            task = workflow.create_task(
                mission.mission_id,
                f"Code change {role}",
                f"change-{role}",
                actor_id=actor_id,
                parents=() if parent is None else (parent,),
                specification=specification,
                acceptance=acceptance,
                max_attempts=1,
                resources=(f"worktree:{prepared.worktree_root}",),
            )
            tasks[role] = task
            parent = task.task_id
    except Exception:
        try:
            workflow.cancel_mission(
                mission.mission_id, actor_id, reason="code-change setup failed"
            )
        finally:
            if prepared is not None:
                remove_code_change_worktree(prepared)
        raise
    return CodeChangeMission(
        mission=mission,
        tasks=tasks,
        prepared=prepared,
        verification_result_path=str(verification_result),
    )


def run_code_change(
    request: Mapping[str, Any],
    *,
    broker: Callable[..., dict[str, Any]] = broker_call,
    control: Callable[..., Any] = control_call,
) -> dict[str, Any]:
    mission_id = _text(request.get("mission_id"), "mission_id")
    task_id = _text(request.get("task_id"), "task_id")
    run_id = _text(request.get("run_id"), "run_id")
    worker_id = _text(request.get("worker_id"), "worker_id")
    goal = _text(request.get("goal"), "goal")
    specification = request.get("specification")
    control_values = request.get("control")
    if not isinstance(specification, Mapping) or not isinstance(
        control_values, Mapping
    ):
        raise ChangeValidationError("worker specification and control must be objects")
    role = _text(specification.get("change_role"), "change_role")
    if role not in _ROLES:
        raise ChangeValidationError("code-change role is invalid")
    prepared = _prepared_from_specification(specification)
    worktree = _real_directory(prepared.worktree_root, "worktree")
    model_request = specification.get("model_request")
    if not isinstance(model_request, Mapping):
        raise ChangeValidationError("model request must be an object")
    model = _text(model_request.get("model"), "model")
    max_tokens = _positive_integer(model_request.get("max_tokens"), "max_tokens")
    max_prompt_chars = _positive_integer(
        model_request.get("max_prompt_chars"), "max_prompt_chars"
    )
    max_patch_bytes = _positive_integer(
        specification.get("max_patch_bytes"), "max_patch_bytes"
    )
    broker_socket = _text(
        specification.get("model_broker_socket"), "model_broker_socket"
    )
    control_socket = _text(control_values.get("socket_path"), "control socket")
    token = _text(control_values.get("token"), "run token")
    control(control_socket, token, "heartbeat", {"lease_seconds": 30})

    parent: dict[str, Any] | None = None
    patch = b""
    source_patch_artifact_id: str | None = None
    if role != "planner":
        expected_parent = _ROLES[_ROLES.index(role) - 1]
        parent = _parent_result(
            request, expected_parent, control_socket, token, control
        )
        if role in {"reviewer", "verifier"}:
            source_patch_artifact_id = _text(
                parent.get("patch_artifact_id"), "patch artifact ID"
            )
            patch = _read_artifact(
                control_socket, token, source_patch_artifact_id, control
            )
            expected_patch_sha = _text(parent.get("patch_sha256"), "patch SHA-256")
            if hashlib.sha256(patch).hexdigest() != expected_patch_sha:
                raise ChangeValidationError("parent patch artifact hash does not match")

    messages, schema = _change_prompt(
        role,
        goal,
        worktree,
        parent,
        patch,
        max_prompt_chars=max_prompt_chars,
    )
    if sum(len(message["content"]) for message in messages) > max_prompt_chars:
        raise ChangeValidationError(
            "rendered code-change prompt exceeds the task limit"
        )
    response = broker(
        broker_socket,
        {
            "request_id": f"{run_id}:{role}",
            "control_socket": control_socket,
            "run_token": token,
            "messages": messages,
            "max_tokens": max_tokens,
            "response_schema": schema,
        },
        timeout_seconds=120,
    )
    if response.get("model") != model:
        raise ChangeValidationError("broker returned an unexpected model")
    try:
        generated = json.loads(response["content"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ChangeValidationError("model did not return valid JSON") from exc

    artifact_ids: list[str] = []
    outcome = "candidate_complete"
    if role == "planner":
        result = _planner_result(worktree, generated)
    elif role == "implementer":
        assert parent is not None
        planned_paths = _planned_paths(parent)
        originals = {path: (worktree / path).read_bytes() for path in planned_paths}
        try:
            receipts = apply_model_edits(
                worktree,
                planned_paths,
                generated.get("edits") if isinstance(generated, dict) else None,
                max_total_bytes=max_patch_bytes,
            )
            captured = capture_code_change_patch(
                prepared,
                planned_paths,
                max_patch_bytes=max_patch_bytes,
            )
        except Exception:
            for path, content in originals.items():
                candidate = worktree / path
                if candidate.is_file() and not candidate.is_symlink():
                    _atomic_write(candidate, content, candidate.stat().st_mode)
            raise
        patch_artifact_id = _put_artifact(
            control_socket,
            token,
            control,
            filename="change.patch",
            media_type="text/x-diff",
            content=captured.content,
            dedupe_key=f"{run_id}:patch",
        )
        artifact_ids.append(patch_artifact_id)
        result = {
            "change_role": role,
            "summary": _generated_text(generated, "summary"),
            "changed_paths": list(captured.changed_paths),
            "edit_receipts": [
                {
                    "path": receipt.path,
                    "before_sha256": receipt.before_sha256,
                    "after_sha256": receipt.after_sha256,
                }
                for receipt in receipts
            ],
            "patch_artifact_id": patch_artifact_id,
            "patch_sha256": captured.sha256,
            "base_commit": captured.base_commit,
        }
    else:
        assert parent is not None and source_patch_artifact_id is not None
        verdict, issues = _verdict_result(generated)
        result = {
            "change_role": role,
            "verdict": verdict,
            "issues": issues,
            "patch_artifact_id": source_patch_artifact_id,
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "changed_paths": list(parent.get("changed_paths", [])),
        }
        if verdict == "fail":
            outcome = "failed"
        elif role == "verifier":
            planned_paths = [_relative_path(value) for value in result["changed_paths"]]
            captured = capture_code_change_patch(
                prepared,
                planned_paths,
                max_patch_bytes=max_patch_bytes,
            )
            if captured.content != patch:
                raise ChangeValidationError("worktree patch changed after review")
            final_patch_artifact_id = _put_artifact(
                control_socket,
                token,
                control,
                filename="verified-change.patch",
                media_type="text/x-diff",
                content=captured.content,
                dedupe_key=f"{run_id}:verified-patch",
            )
            artifact_ids.append(final_patch_artifact_id)
            result["source_patch_artifact_id"] = source_patch_artifact_id
            result["patch_artifact_id"] = final_patch_artifact_id
            _write_verification_files(specification, result, captured.content)

    result.update(
        {
            "mission_id": mission_id,
            "task_id": task_id,
            "run_id": run_id,
            "worker_id": worker_id,
            "model": model,
            "provider_usage": response.get("usage", {}),
        }
    )
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result_artifact_id = _put_artifact(
        control_socket,
        token,
        control,
        filename=f"{role}-result.json",
        media_type="application/json",
        content=encoded,
        dedupe_key=f"{run_id}:{role}:result",
    )
    artifact_ids.append(result_artifact_id)
    control(
        control_socket,
        token,
        "message.publish",
        {
            "topic": f"mission.{mission_id}.change.{role}",
            "kind": "review_note",
            "body": encoded.decode("utf-8"),
            "data": {"change_role": role, "result_artifact_id": result_artifact_id},
            "artifact_refs": artifact_ids,
            "dedupe_key": f"{run_id}:{role}:message",
        },
    )
    control(control_socket, token, "heartbeat", {"lease_seconds": 30})
    summary = (
        str(result.get("summary"))
        if role in {"planner", "implementer"}
        else f"{role} accepted the patch"
        if outcome == "candidate_complete"
        else f"{role} rejected the patch"
    )
    evidence = [{"kind": "model_request", "value": f"{model}:{role}", "exit_code": 0}]
    if role in {"implementer", "verifier"} and outcome == "candidate_complete":
        evidence.append(
            {
                "kind": "patch",
                "value": str(result["patch_sha256"]),
                "exit_code": 0,
            }
        )
    return {
        "outcome": outcome,
        "summary": summary,
        "artifact_ids": artifact_ids,
        "evidence": evidence,
        "fact_proposals": [],
        "residual_risks": []
        if outcome == "candidate_complete"
        else list(result.get("issues", [])),
        "requested_followups": [],
    }


def _prepared_from_specification(
    specification: Mapping[str, Any],
) -> PreparedCodeChange:
    return PreparedCodeChange(
        repository_root=_text(specification.get("repository_root"), "repository_root"),
        worktree_root=_text(specification.get("worktree_root"), "worktree_root"),
        base_commit=_text(specification.get("base_commit"), "base_commit"),
        source_digest=_text(specification.get("source_digest"), "source_digest"),
        worktree_gitfile_sha256=_text(
            specification.get("worktree_gitfile_sha256"), "worktree_gitfile_sha256"
        ),
    )


def _change_prompt(
    role: str,
    goal: str,
    worktree: Path,
    parent: Mapping[str, Any] | None,
    patch: bytes,
    *,
    max_prompt_chars: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    system = (
        "You are one role in a bounded code-change loop. Return only JSON matching the schema. "
        "Keep the change small and satisfy the stated goal."
    )
    if role == "planner":
        evidence = collect_repository_evidence(
            worktree,
            "planner",
            max_chars=max(512, min(12_000, max_prompt_chars - 2_000)),
        )
        user = (
            f"Goal: {goal}\nSelect one to four existing text files and give a concrete edit instruction "
            f"for each.\n{evidence}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], _planner_schema()
    if role == "implementer":
        assert parent is not None
        sources = []
        for path in _planned_paths(parent):
            source_bytes = (worktree / path).read_bytes()
            content = source_bytes.decode("utf-8")
            sources.append(
                {
                    "path": path,
                    "expected_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "content": content,
                }
            )
        user = (
            f"Goal: {goal}\nPlan: {json.dumps(parent, separators=(',', ':'))}\n"
            "Return complete replacement content for every file that must change. Copy each supplied "
            "expected_sha256 exactly.\n"
            f"Files: {json.dumps(sources, separators=(',', ':'))}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], _implementer_schema()
    assert parent is not None
    patch_text = patch.decode("utf-8", errors="replace")
    user = (
        f"Goal: {goal}\nPrevious result: {json.dumps(parent, separators=(',', ':'))}\n"
        f"Patch:\n{patch_text}\n"
        f"Act as the {role}. Return pass only when the patch satisfies the goal and is internally consistent."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], _verdict_schema()


def _planner_result(worktree: Path, generated: Any) -> dict[str, Any]:
    if not isinstance(generated, dict) or set(generated) != {"summary", "changes"}:
        raise ChangeValidationError("planner result fields are invalid")
    changes = generated.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 4:
        raise ChangeValidationError("planner must select between one and four changes")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "instruction"}:
            raise ChangeValidationError("planner change fields are invalid")
        path = _relative_path(change.get("path"))
        if path in seen:
            raise ChangeValidationError("planner paths must be unique")
        seen.add(path)
        candidate = (worktree / path).resolve()
        if (
            not candidate.is_relative_to(worktree)
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            raise ChangeValidationError("planner selected an invalid file")
        if (
            _run_git(worktree, "ls-files", "--error-unmatch", "--", path).returncode
            != 0
        ):
            raise ChangeValidationError("planner selected an untracked file")
        try:
            candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ChangeValidationError("planner selected a non-text file") from exc
        normalized.append(
            {
                "path": path,
                "instruction": _text(change.get("instruction"), "edit instruction"),
            }
        )
    return {
        "change_role": "planner",
        "summary": _generated_text(generated, "summary"),
        "changes": normalized,
    }


def _planned_paths(planner_result: Mapping[str, Any]) -> list[str]:
    changes = planner_result.get("changes")
    if not isinstance(changes, list):
        raise ChangeValidationError("planner handoff is missing changes")
    paths = []
    for change in changes:
        if not isinstance(change, Mapping):
            raise ChangeValidationError("planner handoff contains an invalid change")
        paths.append(_relative_path(change.get("path")))
    if not paths:
        raise ChangeValidationError("planner handoff is empty")
    return paths


def _verdict_result(generated: Any) -> tuple[str, list[str]]:
    if not isinstance(generated, dict) or set(generated) != {"verdict", "issues"}:
        raise ChangeValidationError("review result fields are invalid")
    verdict = generated.get("verdict")
    issues = generated.get("issues")
    if (
        verdict not in {"pass", "fail"}
        or not isinstance(issues, list)
        or len(issues) > 20
    ):
        raise ChangeValidationError("review verdict is invalid")
    normalized = [_text(issue, "review issue") for issue in issues]
    if verdict == "pass" and normalized:
        raise ChangeValidationError("passing review must not contain issues")
    if verdict == "fail" and not normalized:
        raise ChangeValidationError("failed review must explain at least one issue")
    return str(verdict), normalized


def _parent_result(
    request: Mapping[str, Any],
    expected_role: str,
    control_socket: str,
    token: str,
    control: Callable[..., Any],
) -> dict[str, Any]:
    context = request.get("context")
    parents = context.get("parent_handoffs") if isinstance(context, Mapping) else None
    if not isinstance(parents, list) or len(parents) != 1:
        raise ChangeValidationError(
            "code-change role requires exactly one parent handoff"
        )
    metadata = parents[0].get("metadata") if isinstance(parents[0], Mapping) else None
    artifact_ids = (
        metadata.get("artifact_ids") if isinstance(metadata, Mapping) else None
    )
    if not isinstance(artifact_ids, list) or not artifact_ids:
        raise ChangeValidationError("parent handoff has no artifacts")
    for artifact_id in artifact_ids:
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        content = _read_artifact(control_socket, token, artifact_id, control)
        if len(content) > 64_000:
            continue
        try:
            value = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict) and value.get("change_role") == expected_role:
            return value
    raise ChangeValidationError(f"parent {expected_role} result artifact is missing")


def _read_artifact(
    control_socket: str,
    token: str,
    artifact_id: str,
    control: Callable[..., Any],
) -> bytes:
    value = control(
        control_socket,
        token,
        "artifact.read",
        {"artifact_id": artifact_id},
    )
    encoded = value.get("content_base64") if isinstance(value, Mapping) else None
    if not isinstance(encoded, str):
        raise ChangeValidationError("artifact read returned invalid content")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ChangeValidationError("artifact read returned invalid base64") from exc


def _put_artifact(
    control_socket: str,
    token: str,
    control: Callable[..., Any],
    *,
    filename: str,
    media_type: str,
    content: bytes,
    dedupe_key: str,
) -> str:
    value = control(
        control_socket,
        token,
        "artifact.put",
        {
            "filename": filename,
            "media_type": media_type,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "dedupe_key": dedupe_key,
        },
    )
    artifact_id = value.get("artifact_id") if isinstance(value, Mapping) else None
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ChangeValidationError("artifact store returned an invalid result")
    return artifact_id


def _write_verification_files(
    specification: Mapping[str, Any],
    result: Mapping[str, Any],
    patch: bytes,
) -> None:
    result_root = _real_directory(
        _text(specification.get("result_root"), "result_root"), "result root"
    )
    result_path = (
        Path(
            _text(
                specification.get("verification_result_path"),
                "verification_result_path",
            )
        )
        .expanduser()
        .resolve()
    )
    if not result_path.is_relative_to(result_root) or result_path.parent.is_symlink():
        raise ChangeValidationError(
            "verification result path is outside the result root"
        )
    result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    patch_path = result_path.with_suffix(".patch")
    verification = dict(result)
    verification["patch_path"] = str(patch_path)
    _atomic_write(patch_path, patch, 0o600)
    _atomic_write(
        result_path,
        json.dumps(verification, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        0o600,
    )


def _planner_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 1_000},
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": 1_000},
                        "instruction": {"type": "string", "maxLength": 2_000},
                    },
                    "required": ["path", "instruction"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "changes"],
        "additionalProperties": False,
    }


def _implementer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 1_000},
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": 1_000},
                        "expected_sha256": {
                            "type": "string",
                            "pattern": "^[a-f0-9]{64}$",
                        },
                        "content": {"type": "string", "maxLength": 24_000},
                    },
                    "required": ["path", "expected_sha256", "content"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "edits"],
        "additionalProperties": False,
    }


def _verdict_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "issues": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 1_000},
            },
        },
        "required": ["verdict", "issues"],
        "additionalProperties": False,
    }


def _generated_text(generated: Any, field: str) -> str:
    if not isinstance(generated, Mapping):
        raise ChangeValidationError("model result must be an object")
    return _text(generated.get(field), field)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64_000:
        raise ChangeValidationError(f"{name} must be a non-empty bounded string")
    return value.strip()


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChangeValidationError(f"{name} must be a positive integer")
    return value


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_000 or "\\" in value:
        raise ChangeValidationError("planned path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ChangeValidationError("planned path is invalid")
    return path.as_posix()


def _handoff_worktree(
    prepared: PreparedCodeChange,
    result_directory: Path,
    worker_user: str | None,
) -> None:
    if worker_user is None:
        return
    if not isinstance(worker_user, str) or not worker_user.strip():
        raise ChangeValidationError("worker user must be a non-empty account name")
    try:
        account = pwd.getpwnam(worker_user.strip())
    except KeyError as exc:
        raise ChangeValidationError(f"unknown worker account: {worker_user}") from exc
    worktree = Path(prepared.worktree_root)
    for directory, directories, files in os.walk(worktree, followlinks=False):
        directory_path = Path(directory)
        os.chown(directory_path, account.pw_uid, account.pw_gid)
        os.chmod(directory_path, directory_path.stat().st_mode | stat.S_IRWXU)
        for name in (*directories, *files):
            path = directory_path / name
            if path == worktree / ".git":
                continue
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
            if not path.is_symlink():
                owner_permissions = (
                    stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR
                )
                os.chmod(path, path.stat().st_mode | owner_permissions)
    os.chown(result_directory, account.pw_uid, account.pw_gid)
    os.chmod(result_directory, 0o700)


def _real_directory(value: str | Path, name: str) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise ChangeValidationError(f"{name} must be a real directory")
    return raw.resolve()


def _command(value: Sequence[str], name: str) -> tuple[str, ...]:
    if (
        isinstance(value, (str, bytes))
        or not value
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise ChangeValidationError(f"{name} must be a non-empty command array")
    return tuple(value)


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository}",
            "-C",
            str(repository),
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    completed = _run_git(repository, *arguments)
    if completed.returncode != 0:
        raise ChangeValidationError(_git_error(completed, "Git command failed"))
    return completed.stdout


def _git_text(repository: Path, *arguments: str, error: str) -> str:
    completed = _run_git(repository, *arguments)
    if completed.returncode != 0:
        raise ChangeValidationError(_git_error(completed, error))
    return completed.stdout.decode("utf-8").strip()


def _git_error(completed: subprocess.CompletedProcess[bytes], fallback: str) -> str:
    detail = completed.stderr.decode("utf-8", errors="replace").strip()
    return f"{fallback}: {detail}" if detail else fallback


def _remove_worktree(repository: Path, worktree: Path) -> None:
    completed = _run_git(repository, "worktree", "remove", "--force", str(worktree))
    if completed.returncode != 0:
        raise ChangeValidationError(
            _git_error(completed, "Git worktree removal failed")
        )


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ChangeValidationError("worker request must be an object")
        result = run_code_change(request)
    except Exception as exc:
        result = {
            "outcome": "failed",
            "summary": f"code-change worker failed: {type(exc).__name__}: {exc}",
            "artifact_ids": [],
            "evidence": [],
            "fact_proposals": [],
            "residual_risks": ["code-change output was not accepted"],
            "requested_followups": [],
        }
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
