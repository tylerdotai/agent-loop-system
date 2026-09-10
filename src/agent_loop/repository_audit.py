from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .compact_protocol import (
    CompactProtocolError,
    Packet,
    compact_mapping,
    decode_result_packet,
    encode_evidence_packet,
    encode_result_packet,
    make_record,
    parse_packet,
)
from .model_broker import broker_call
from .worker_api import control_call


class AuditValidationError(ValueError):
    """Raised when repository evidence or model output fails closed."""


_SPECIALISTS = {"security", "testing", "architecture"}
_ROLES = {"planner", *_SPECIALISTS, "synthesis", "verifier"}
_SKIP_PARTS = {
    ".agent-loop",
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_TEXT_SUFFIXES = {".py", ".md", ".toml", ".json", ".yaml", ".yml", ".service", ".txt"}
_ROLE_PRIORITY = {
    "planner": ("README.md", "AGENTS.md", "pyproject.toml", "docs/ARCHITECTURE.md"),
    "security": (
        "SECURITY.md",
        "src/agent_loop/worker_api.py",
        "src/agent_loop/model_broker.py",
        "src/agent_loop/runner_adapter.py",
        "src/agent_loop/action_broker.py",
        "deploy/systemd/agent-loop-worker@.service",
    ),
    "testing": (
        "pyproject.toml",
        "tests/test_model_broker.py",
        "tests/test_runner_adapter.py",
        "tests/test_workflow_control_plane.py",
        ".github/workflows/ci.yml",
    ),
    "architecture": (
        "README.md",
        "docs/ARCHITECTURE.md",
        "src/agent_loop/coordinator.py",
        "src/agent_loop/workflow.py",
        "src/agent_loop/message_board.py",
    ),
    "synthesis": ("README.md", "SECURITY.md", "docs/ARCHITECTURE.md"),
    "verifier": ("SECURITY.md", "docs/ARCHITECTURE.md"),
}


def collect_repository_evidence(repository_root: str | Path, role: str, *, max_chars: int) -> str:
    root = Path(repository_root).expanduser().resolve()
    if role not in _ROLES:
        raise AuditValidationError("audit role is not allowed")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 256:
        raise AuditValidationError("evidence character limit is invalid")
    if not root.is_dir() or root.is_symlink():
        raise AuditValidationError("repository root must be a real directory")
    candidates: list[Path] = []
    seen: set[Path] = set()
    for relative in _ROLE_PRIORITY[role]:
        path = root / relative
        if path.is_file() and not path.is_symlink():
            candidates.append(path)
            seen.add(path)
    for path in sorted(root.rglob("*")):
        if path in seen or not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in _SKIP_PARTS or part.startswith(".") for part in relative.parts):
            continue
        lowered = path.name.lower()
        if lowered.startswith(".env") or any(word in lowered for word in ("credential", "private_key")):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES or path.stat().st_size > 200_000:
            continue
        candidates.append(path)
    output: list[str] = []
    used = 0
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        block = f"[FILE {relative}]\n{content.strip()}\n[/FILE]\n"
        remaining = max_chars - used
        if remaining <= len(f"[FILE {relative}]\n[/FILE]\n"):
            break
        if len(block) > remaining:
            block = block[: remaining - 10] + "\n[/FILE]\n"
        output.append(block)
        used += len(block)
        if used >= max_chars:
            break
    if not output:
        raise AuditValidationError("repository did not contain admissible text evidence")
    return "".join(output)


def repository_digest(repository_root: str | Path) -> str:
    root = Path(repository_root).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise AuditValidationError("repository root must be a real directory")
    digest = hashlib.sha256(b"ALS-REPOSITORY-DIGEST-v2\0")
    included_files = 0
    for path, relative in _walk_repository(root):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise AuditValidationError(f"repository entry is unreadable: {relative.as_posix()}") from exc
        if path.is_symlink():
            kind = b"link"
        elif path.is_dir():
            kind = b"directory"
        elif path.is_file():
            kind = b"file"
            included_files += 1
        else:
            kind = b"special"
        _digest_field(digest, kind)
        _digest_field(digest, relative.as_posix().encode("utf-8", errors="surrogateescape"))
        _digest_field(digest, f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"))
        if kind == b"link":
            _digest_field(digest, os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif kind == b"file":
            digest.update(metadata.st_size.to_bytes(8, "big", signed=False))
            bytes_read = 0
            try:
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                        bytes_read += len(chunk)
            except OSError as exc:
                raise AuditValidationError(
                    f"repository file is unreadable: {relative.as_posix()}"
                ) from exc
            if bytes_read != metadata.st_size:
                raise AuditValidationError(f"repository file changed while hashing: {relative.as_posix()}")
        else:
            _digest_field(digest, b"")
    if included_files == 0:
        raise AuditValidationError("repository digest has no files")
    return digest.hexdigest()


def _walk_repository(root: Path) -> Iterator[tuple[Path, Path]]:
    def visit(directory: Path, relative_directory: Path) -> Iterator[tuple[Path, Path]]:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        except OSError as exc:
            label = relative_directory.as_posix() if relative_directory.parts else "."
            raise AuditValidationError(f"repository directory is unreadable: {label}") from exc
        for child in children:
            if child.name in _SKIP_PARTS:
                continue
            relative = relative_directory / child.name
            path = Path(child.path)
            yield path, relative
            try:
                is_directory = child.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise AuditValidationError(
                    f"repository entry is unreadable: {relative.as_posix()}"
                ) from exc
            if is_directory:
                yield from visit(path, relative)

    yield from visit(root, Path())


def _digest_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big", signed=False))
    digest.update(value)


def build_evidence_catalog(
    repository_root: str | Path,
    role: str,
    *,
    max_chars: int,
) -> tuple[str, dict[str, tuple[str, str]]]:
    raw = collect_repository_evidence(repository_root, role, max_chars=max_chars)
    catalog: dict[str, tuple[str, str]] = {}
    rendered: list[str] = []
    rendered_chars = 0
    current_file: str | None = None
    for line in raw.splitlines():
        if line.startswith("[FILE ") and line.endswith("]"):
            current_file = line[6:-1]
            continue
        if line == "[/FILE]":
            current_file = None
            continue
        evidence = line.strip()
        if current_file is None or not evidence:
            continue
        evidence = evidence[:500]
        evidence_id = f"E{len(catalog) + 1:04d}"
        rendered_line = f"[{evidence_id}] file={current_file} :: {evidence}"
        separator_chars = 1 if rendered else 0
        if rendered_chars + separator_chars + len(rendered_line) > max_chars:
            break
        catalog[evidence_id] = (current_file, evidence)
        rendered.append(rendered_line)
        rendered_chars += separator_chars + len(rendered_line)
        if len(catalog) >= 240:
            break
    if not catalog:
        raise AuditValidationError("repository evidence catalog is empty")
    return "\n".join(rendered), catalog


def validate_findings(repository_root: str | Path, findings: Any) -> list[dict[str, str]]:
    root = Path(repository_root).expanduser().resolve()
    if not isinstance(findings, list) or len(findings) > 20:
        raise AuditValidationError("findings must be a bounded array")
    validated: list[dict[str, str]] = []
    required = {"severity", "file", "evidence", "finding", "recommendation"}
    for item in findings:
        if not isinstance(item, dict) or set(item) != required:
            raise AuditValidationError("each finding must contain the exact required fields")
        if item.get("severity") not in {"critical", "high", "moderate", "low", "info"}:
            raise AuditValidationError("finding severity is invalid")
        normalized: dict[str, str] = {}
        for field in required:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 2_000:
                raise AuditValidationError(f"finding {field} must be a bounded string")
            normalized[field] = value.strip()
        candidate = (root / normalized["file"]).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise AuditValidationError("finding file is outside the repository")
        if not candidate.is_file() or candidate.is_symlink():
            raise AuditValidationError("finding file must be a real repository file")
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AuditValidationError("finding file is not readable text") from exc
        if normalized["evidence"] not in content:
            raise AuditValidationError("finding evidence must be an exact substring of the cited file")
        normalized["file"] = candidate.relative_to(root).as_posix()
        validated.append(normalized)
    return validated


def resolve_evidence_findings(
    repository_root: str | Path,
    evidence_catalog: dict[str, tuple[str, str]],
    generated_findings: Any,
) -> list[dict[str, str]]:
    if not isinstance(generated_findings, list) or len(generated_findings) > 8:
        raise AuditValidationError("specialist findings must be a bounded array")
    canonical: list[dict[str, str]] = []
    required = {"severity", "evidence_id", "finding", "recommendation"}
    for item in generated_findings:
        if not isinstance(item, dict) or set(item) != required:
            raise AuditValidationError("specialist finding fields are invalid")
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or evidence_id not in evidence_catalog:
            raise AuditValidationError("specialist selected an unknown evidence ID")
        severity = item.get("severity")
        if severity not in {"critical", "high", "moderate", "low", "info"}:
            raise AuditValidationError("specialist selected an invalid severity")
        file_name, exact_evidence = evidence_catalog[evidence_id]
        canonical.append(
            {
                "severity": str(severity),
                "file": file_name,
                "evidence": exact_evidence,
                "finding": _text(item.get("finding"), "specialist finding"),
                "recommendation": _text(
                    item.get("recommendation"),
                    "specialist recommendation",
                ),
            }
        )
    return validate_findings(repository_root, canonical)


def run_audit(
    request: Mapping[str, Any],
    *,
    broker: Callable[..., dict[str, Any]] = broker_call,
    control: Callable[..., Any] = control_call,
) -> dict[str, Any]:
    mission_id = _text(request.get("mission_id"), "mission_id")
    task_id = _text(request.get("task_id"), "task_id")
    run_id = _text(request.get("run_id"), "run_id")
    worker_id = _text(request.get("worker_id"), "worker_id")
    specification = request.get("specification")
    control_values = request.get("control")
    workspace = Path(_text(request.get("workspace"), "workspace")).expanduser().resolve()
    if not isinstance(specification, dict) or not isinstance(control_values, dict):
        raise AuditValidationError("request specification and control must be objects")
    role = _text(specification.get("audit_role"), "audit_role")
    if role not in _ROLES:
        raise AuditValidationError("audit role is not allowed")
    repository = Path(_text(specification.get("repository_root"), "repository_root")).expanduser().resolve()
    broker_socket = _text(specification.get("model_broker_socket"), "model_broker_socket")
    model_request = specification.get("model_request")
    if not isinstance(model_request, dict):
        raise AuditValidationError("model_request must be an object")
    model = _text(model_request.get("model"), "model")
    protocol = specification.get("communication_protocol", "JSON")
    if protocol not in {"JSON", "ACS1"}:
        raise AuditValidationError("communication_protocol must be JSON or ACS1")
    max_tokens = _positive_integer(model_request.get("max_tokens"), "max_tokens")
    max_prompt_chars = _positive_integer(model_request.get("max_prompt_chars"), "max_prompt_chars")
    control_socket = _text(control_values.get("socket_path"), "control socket")
    token = _text(control_values.get("token"), "run token")
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    control(control_socket, token, "heartbeat", {"lease_seconds": 30})
    prior_messages = control(
        control_socket,
        token,
        "message.list",
        {"topic_prefix": f"mission.{mission_id}.audit", "limit": 100},
    )
    if not isinstance(prior_messages, list):
        raise AuditValidationError("message board returned invalid prior results")
    prior_results = _prior_results(prior_messages)
    evidence_budget = max(512, min(max_prompt_chars - 3_000, 12_000))
    evidence_catalog: dict[str, tuple[str, str]] = {}
    if role in _SPECIALISTS:
        repository_evidence, evidence_catalog = build_evidence_catalog(
            repository,
            role,
            max_chars=evidence_budget,
        )
    else:
        repository_evidence = collect_repository_evidence(
            repository,
            role,
            max_chars=evidence_budget,
        )
    try:
        messages, schema, evidence_catalog = _prompt(
            role,
            repository_evidence,
            prior_results,
            evidence_catalog,
            protocol=protocol,
        )
    except CompactProtocolError as exc:
        raise AuditValidationError(f"compact protocol construction failed: {exc}") from exc
    if sum(len(message["content"]) for message in messages) > max_prompt_chars:
        raise AuditValidationError("final rendered model prompt exceeds task limit")
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
        raise AuditValidationError("broker returned an unexpected model")
    try:
        generated = json.loads(response["content"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AuditValidationError("model did not return valid JSON") from exc
    result = _finalize_role_result(
        role,
        repository,
        generated,
        prior_results,
        evidence_catalog,
    )
    result.update(
        {
            "role": role,
            "worker_id": worker_id,
            "mission_id": mission_id,
            "task_id": task_id,
            "run_id": run_id,
            "model": model,
            "provider_usage": response.get("usage", {}),
        }
    )
    message_body, message_protocol = _encode_result_message(result, protocol)
    encoded = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
    artifact_path = workspace / f"{role}-result.json"
    artifact_path.write_bytes(encoded)
    os.chmod(artifact_path, 0o600)
    artifact = control(
        control_socket,
        token,
        "artifact.put",
        {
            "filename": artifact_path.name,
            "media_type": "application/json",
            "content_base64": base64.b64encode(encoded).decode("ascii"),
            "dedupe_key": f"{run_id}:{role}:result",
        },
    )
    if not isinstance(artifact, dict) or not isinstance(artifact.get("artifact_id"), str):
        raise AuditValidationError("artifact store returned an invalid result")
    control(
        control_socket,
        token,
        "message.publish",
        {
            "topic": f"mission.{mission_id}.audit.{role}",
            "kind": "review_note",
            "body": message_body,
            "data": {
                "artifact_id": artifact["artifact_id"],
                "role": role,
                "protocol": message_protocol,
            },
            "artifact_refs": [artifact["artifact_id"]],
            "dedupe_key": f"{run_id}:{role}:message",
        },
    )
    control(control_socket, token, "heartbeat", {"lease_seconds": 30})
    return {
        "outcome": "candidate_complete",
        "summary": _summary(role, result),
        "artifact_ids": [artifact["artifact_id"]],
        "evidence": [
            {
                "kind": "model_request",
                "value": f"{model}:{role}",
                "exit_code": 0,
            }
        ],
        "fact_proposals": [],
        "residual_risks": [],
        "requested_followups": [],
    }


def _encode_result_message(result: dict[str, Any], protocol: str) -> tuple[str, str]:
    legacy = json.dumps(result, separators=(",", ":"))
    if len(legacy) > 64_000:
        raise AuditValidationError("canonical result exceeds durable message limit")
    if protocol == "JSON":
        return legacy, "JSON"
    if protocol != "ACS1":
        raise AuditValidationError("unsupported communication protocol")
    try:
        return encode_result_packet(result), "ACS1"
    except CompactProtocolError as exc:
        if str(exc) != "packet exceeds maximum size":
            raise
        return legacy, "JSON"


def _prompt(
    role: str,
    repository_evidence: str,
    prior_results: list[dict[str, Any]],
    evidence_catalog: dict[str, tuple[str, str]],
    *,
    protocol: str = "JSON",
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, tuple[str, str]]]:
    if protocol == "ACS1":
        return _compact_prompt(role, repository_evidence, prior_results, evidence_catalog)
    if protocol != "JSON":
        raise AuditValidationError("unsupported communication protocol")
    base = (
        "You are one worker in a governed read-only repository audit. Return only JSON matching "
        "the schema. Never claim a file fact without exact quoted evidence. Do not propose external actions."
    )
    if role == "planner":
        user = (
            "Select no more than six bounded audit areas. Keep the summary under 1,000 characters and "
            f"each area under 500 characters.\n{repository_evidence}"
        )
        return (
            [{"role": "system", "content": base}, {"role": "user", "content": user}],
            _planner_schema(),
            evidence_catalog,
        )
    if role in _SPECIALISTS:
        user = (
            f"Perform the {role} audit. For every finding, select one evidence_id from the supplied "
            f"catalog. Zero findings is allowed; invented evidence IDs are not.\n{repository_evidence}\n"
            f"Prior plan: {_bounded_json(prior_results, 2_000)}"
        )
        return (
            [{"role": "system", "content": base}, {"role": "user", "content": user}],
            _findings_schema(tuple(evidence_catalog)),
            evidence_catalog,
        )
    if role == "synthesis":
        findings = _canonical_findings(prior_results)
        user = (
            "Write a concise executive summary and choose an overall risk. Return finding_order as indexes "
            "into the supplied canonical list; do not create or rewrite findings.\n"
            f"Canonical findings: {_bounded_json(findings, 12_000)}"
        )
        return (
            [{"role": "system", "content": base}, {"role": "user", "content": user}],
            _synthesis_schema(len(findings)),
            evidence_catalog,
        )
    reports = [result for result in prior_results if result.get("role") == "synthesis"]
    if not reports:
        raise AuditValidationError("verifier requires a synthesis result")
    user = (
        "Review this synthesized audit for internal consistency. A pass means cited evidence has already "
        "passed deterministic validation and the report contains no unsupported additions.\n"
        f"Report: {_bounded_json(reports[-1], 12_000)}"
    )
    return (
        [{"role": "system", "content": base}, {"role": "user", "content": user}],
        _verifier_schema(),
        evidence_catalog,
    )


def _compact_prompt(
    role: str,
    repository_evidence: str,
    prior_results: list[dict[str, Any]],
    evidence_catalog: dict[str, tuple[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, tuple[str, str]]]:
    if role == "planner":
        return _prompt(
            role,
            repository_evidence,
            prior_results,
            evidence_catalog,
            protocol="JSON",
        )
    if role in _SPECIALISTS:
        base = (
            "Read-only audit. ACS1: Q task; P path; E evidence-id/path-id/exact text. "
            "Return schema JSON. Cite supplied E IDs only. No actions."
        )
        packet, wire_catalog = encode_evidence_packet(
            evidence_catalog,
            domain="AUD",
            query=f"{role} audit; zero findings allowed; cite E IDs only",
        )
        return (
            [{"role": "system", "content": base}, {"role": "user", "content": packet}],
            _findings_schema(tuple(wire_catalog)),
            wire_catalog,
        )
    if role == "synthesis":
        findings = _canonical_findings(prior_results)
        if not findings:
            return _prompt(
                role,
                repository_evidence,
                prior_results,
                evidence_catalog,
                protocol="JSON",
            )
        base = (
            "Read-only audit. ACS1 C aliases: v severity,p file,e evidence,d finding,x "
            "recommendation. Return schema JSON. Reorder only; add nothing."
        )
        packet = Packet(
            "CTX",
            (
                make_record("Q", domain="AUD", query="summarize; order canonical findings only"),
                make_record("C", name="findings", value=compact_mapping(findings)),
            ),
        ).encode()
        return (
            [{"role": "system", "content": base}, {"role": "user", "content": packet}],
            _synthesis_schema(len(findings)),
            evidence_catalog,
        )
    return _prompt(
        role,
        repository_evidence,
        prior_results,
        evidence_catalog,
        protocol="JSON",
    )


def _finalize_role_result(
    role: str,
    repository: Path,
    generated: Any,
    prior_results: list[dict[str, Any]],
    evidence_catalog: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    if not isinstance(generated, dict):
        raise AuditValidationError("model result must be an object")
    if role == "planner":
        summary = _text(generated.get("summary"), "planner summary")
        areas = generated.get("audit_areas")
        if not isinstance(areas, list) or not 1 <= len(areas) <= 6:
            raise AuditValidationError("planner audit_areas must contain between 1 and 6 values")
        return {"summary": summary, "audit_areas": [_text(area, "audit area") for area in areas]}
    if role in _SPECIALISTS:
        return {
            "summary": _text(generated.get("summary"), "specialist summary"),
            "findings": resolve_evidence_findings(
                repository,
                evidence_catalog,
                generated.get("findings"),
            ),
        }
    if role == "synthesis":
        findings = _canonical_findings(prior_results)
        order = generated.get("finding_order")
        if not isinstance(order, list) or any(isinstance(index, bool) or not isinstance(index, int) for index in order):
            raise AuditValidationError("finding_order must be an integer array")
        if sorted(order) != list(range(len(findings))):
            raise AuditValidationError("finding_order must contain every canonical finding exactly once")
        ordered = [findings[index] for index in order]
        return {
            "executive_summary": _text(generated.get("executive_summary"), "executive summary"),
            "overall_risk": _risk(generated.get("overall_risk")),
            "findings": ordered,
        }
    verdict = generated.get("verdict")
    issues = generated.get("issues")
    if verdict not in {"pass", "fail"} or not isinstance(issues, list) or len(issues) > 20:
        raise AuditValidationError("verifier result is invalid")
    synthesis = [result for result in prior_results if result.get("role") == "synthesis"][-1]
    validate_findings(repository, synthesis.get("findings"))
    return {
        "verdict": verdict,
        "verified_count": len(synthesis.get("findings", [])),
        "issues": [_text(issue, "verification issue") for issue in issues],
        "deterministic_evidence_check": "passed",
    }


def _prior_results(messages: list[Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("kind") != "review_note":
            continue
        body = message.get("body")
        if not isinstance(body, str) or len(body) > 64_000:
            continue
        try:
            value = (
                decode_result_packet(parse_packet(body))
                if body.startswith("ACS1|")
                else json.loads(body)
            )
        except (json.JSONDecodeError, CompactProtocolError):
            continue
        if isinstance(value, dict):
            results.append(value)
    return results


def _canonical_findings(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for result in results:
        if result.get("role") not in _SPECIALISTS:
            continue
        values = result.get("findings")
        if not isinstance(values, list):
            continue
        for finding in values:
            if not isinstance(finding, dict):
                continue
            key = (str(finding.get("file")), str(finding.get("evidence")), str(finding.get("finding")))
            if key in seen:
                continue
            seen.add(key)
            findings.append(dict(finding))
    return findings[:20]


def _planner_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 1_000},
            "audit_areas": {
                "type": "array",
                "items": {"type": "string", "maxLength": 500},
                "minItems": 1,
                "maxItems": 6,
            },
        },
        "required": ["summary", "audit_areas"],
        "additionalProperties": False,
    }


def _finding_properties(evidence_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "severity": {"type": "string", "enum": ["critical", "high", "moderate", "low", "info"]},
        "evidence_id": {"type": "string", "enum": list(evidence_ids)},
        "finding": {"type": "string", "maxLength": 1_000},
        "recommendation": {"type": "string", "maxLength": 1_000},
    }


def _findings_schema(evidence_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 1_000},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": _finding_properties(evidence_ids),
                    "required": ["severity", "evidence_id", "finding", "recommendation"],
                    "additionalProperties": False,
                },
                "maxItems": 8,
            },
        },
        "required": ["summary", "findings"],
        "additionalProperties": False,
    }


def _synthesis_schema(finding_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "executive_summary": {"type": "string", "maxLength": 1_500},
            "overall_risk": {"type": "string", "enum": ["critical", "high", "moderate", "low"]},
            "finding_order": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": max(0, finding_count - 1)},
                "minItems": finding_count,
                "maxItems": finding_count,
            },
        },
        "required": ["executive_summary", "overall_risk", "finding_order"],
        "additionalProperties": False,
    }


def _verifier_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "issues": {
                "type": "array",
                "items": {"type": "string", "maxLength": 1_000},
                "maxItems": 20,
            },
        },
        "required": ["verdict", "issues"],
        "additionalProperties": False,
    }


def _summary(role: str, result: dict[str, Any]) -> str:
    if role == "synthesis":
        return str(result["executive_summary"])
    if role == "verifier":
        return f"Verifier verdict: {result['verdict']}"
    return str(result["summary"])


def _risk(value: Any) -> str:
    if value not in {"critical", "high", "moderate", "low"}:
        raise AuditValidationError("overall risk is invalid")
    return str(value)


def _bounded_json(value: Any, maximum: int) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)[:maximum]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64_000:
        raise AuditValidationError(f"{name} must be a non-empty bounded string")
    return value.strip()


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AuditValidationError(f"{name} must be a positive integer")
    return value


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise AuditValidationError("worker request must be an object")
        result = run_audit(request)
    except Exception as exc:
        result = {
            "outcome": "failed",
            "summary": f"repository audit failed: {type(exc).__name__}: {exc}",
            "artifact_ids": [],
            "evidence": [],
            "fact_proposals": [],
            "residual_risks": ["audit output was not accepted"],
            "requested_followups": [],
        }
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
