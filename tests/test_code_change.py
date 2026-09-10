from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from agent_loop.code_change import (
    ChangeValidationError,
    _change_prompt,
    apply_model_edits,
    capture_code_change_patch,
    create_code_change_mission,
    prepare_code_change_worktree,
    remove_code_change_worktree,
    run_code_change,
)
from agent_loop.code_change_verify import verify_code_change_result
from agent_loop.action_broker import ActionBroker, BudgetService
from agent_loop.artifacts import ArtifactStore
from agent_loop.coordinator import Coordinator
from agent_loop.control_cli import main as control_main
from agent_loop.message_board import MessageBoard
from agent_loop.model_broker import (
    ModelBroker,
    ModelBrokerPolicy,
    ModelBrokerSocketServer,
)
from agent_loop.persistence import SQLiteStore
from agent_loop.repository_audit import repository_digest
from agent_loop.runner_adapter import JsonSubprocessRunner
from agent_loop.workflow import WorkflowService
from agent_loop.worker_api import ControlSocketServer, RunTokenService, WorkerAPI


def git(
    repository: Path, *arguments: str, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def make_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    assert git(repository, "init", "-q").returncode == 0
    assert git(repository, "config", "user.name", "Test Operator").returncode == 0
    assert (
        git(repository, "config", "user.email", "operator@example.invalid").returncode
        == 0
    )
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "README.md").write_text("# Fixture\n", encoding="utf-8")
    assert git(repository, "add", "service.py", "README.md").returncode == 0
    assert git(repository, "commit", "-q", "-m", "initial").returncode == 0
    return repository


def test_prepare_worktree_pins_clean_head_without_changing_source_checkout(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    before = repository_digest(repository)
    base = git(repository, "rev-parse", "HEAD").stdout.decode().strip()

    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")

    assert prepared.repository_root == str(repository.resolve())
    assert prepared.worktree_root == str((worktrees / "mission-1").resolve())
    assert prepared.base_commit == base
    assert prepared.source_digest == before == repository_digest(repository)
    assert len(prepared.worktree_gitfile_sha256) == 64
    assert (
        git(Path(prepared.worktree_root), "rev-parse", "HEAD").stdout.decode().strip()
        == base
    )
    assert (
        git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout
        == b""
    )

    remove_code_change_worktree(prepared)
    assert not Path(prepared.worktree_root).exists()
    assert repository_digest(repository) == before


def test_prepare_worktree_rejects_dirty_source_symlink_and_path_escape(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    (repository / "service.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ChangeValidationError, match="source repository must be clean"):
        prepare_code_change_worktree(repository, worktrees, "mission-1")

    assert git(repository, "checkout", "--", "service.py").returncode == 0
    alias = tmp_path / "repository-link"
    alias.symlink_to(repository, target_is_directory=True)
    with pytest.raises(ChangeValidationError, match="real directory"):
        prepare_code_change_worktree(alias, worktrees, "mission-2")
    with pytest.raises(ChangeValidationError, match="mission identifier"):
        prepare_code_change_worktree(repository, worktrees, "../escape")


def test_apply_model_edits_requires_planned_tracked_text_and_matching_hash(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")
    worktree = Path(prepared.worktree_root)
    original = (worktree / "service.py").read_bytes()
    expected = hashlib.sha256(original).hexdigest()

    receipts = apply_model_edits(
        worktree,
        ["service.py"],
        [
            {
                "path": "service.py",
                "expected_sha256": expected,
                "content": "VALUE = 2\n",
            }
        ],
        max_total_bytes=1_000,
    )

    assert [receipt.path for receipt in receipts] == ["service.py"]
    assert (worktree / "service.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    with pytest.raises(ChangeValidationError, match="planned path"):
        apply_model_edits(
            worktree,
            ["service.py"],
            [
                {
                    "path": "README.md",
                    "expected_sha256": hashlib.sha256(
                        (worktree / "README.md").read_bytes()
                    ).hexdigest(),
                    "content": "changed\n",
                }
            ],
            max_total_bytes=1_000,
        )
    with pytest.raises(ChangeValidationError, match="hash no longer matches"):
        apply_model_edits(
            worktree,
            ["service.py"],
            [
                {
                    "path": "service.py",
                    "expected_sha256": expected,
                    "content": "VALUE = 3\n",
                }
            ],
            max_total_bytes=1_000,
        )


def test_implementer_prompt_hashes_original_crlf_bytes(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")
    worktree = Path(prepared.worktree_root)
    original = b"VALUE = 1\r\n"
    (worktree / "service.py").write_bytes(original)

    messages, _schema = _change_prompt(
        "implementer",
        "Change VALUE from 1 to 2.",
        worktree,
        {
            "change_role": "planner",
            "summary": "Change the value.",
            "changes": [{"path": "service.py", "instruction": "Change VALUE."}],
        },
        b"",
        max_prompt_chars=16_000,
    )
    files = json.loads(messages[1]["content"].split("Files: ", 1)[1])

    assert files[0]["expected_sha256"] == hashlib.sha256(original).hexdigest()
    assert files[0]["content"].encode("utf-8") == original


def test_capture_patch_accepts_crlf_file_changes(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    original = b"VALUE = 1\r\n"
    (repository / ".gitattributes").write_text("*.py -text\n", encoding="utf-8")
    (repository / "service.py").write_bytes(original)
    assert git(repository, "add", ".gitattributes", "service.py").returncode == 0
    assert git(repository, "commit", "-q", "-m", "use CRLF").returncode == 0
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")
    worktree = Path(prepared.worktree_root)

    apply_model_edits(
        worktree,
        ["service.py"],
        [
            {
                "path": "service.py",
                "expected_sha256": hashlib.sha256(original).hexdigest(),
                "content": "VALUE = 2\r\n",
            }
        ],
        max_total_bytes=1_000,
    )

    patch = capture_code_change_patch(
        prepared,
        ["service.py"],
        max_patch_bytes=10_000,
    )
    assert b"+VALUE = 2\r\n" in patch.content


def test_capture_patch_is_bounded_applicable_and_source_checkout_stays_unchanged(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")
    worktree = Path(prepared.worktree_root)
    source_digest = repository_digest(repository)
    (worktree / "service.py").write_text("VALUE = 2\n", encoding="utf-8")

    patch = capture_code_change_patch(prepared, ["service.py"], max_patch_bytes=10_000)

    assert patch.changed_paths == ("service.py",)
    assert patch.sha256 == hashlib.sha256(patch.content).hexdigest()
    assert b"-VALUE = 1" in patch.content
    assert b"+VALUE = 2" in patch.content
    assert (
        git(
            repository, "apply", "--check", "--binary", "-", input_bytes=patch.content
        ).returncode
        == 0
    )
    assert repository_digest(repository) == source_digest
    assert (
        git(Path(prepared.worktree_root), "rev-parse", "HEAD").stdout.decode().strip()
        == prepared.base_commit
    )


def test_capture_patch_rejects_changes_outside_the_plan(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")
    worktree = Path(prepared.worktree_root)
    (worktree / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ChangeValidationError, match="outside the approved plan"):
        capture_code_change_patch(prepared, ["README.md"], max_patch_bytes=10_000)


def test_create_code_change_mission_builds_exact_four_task_chain(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    results = tmp_path / "results"
    worktrees.mkdir()
    results.mkdir()
    workflow = WorkflowService(SQLiteStore(tmp_path / "control.db"))
    model_request = {
        "model": "local-model",
        "max_tokens": 1_024,
        "max_prompt_chars": 16_000,
        "temperature": 0,
    }

    created = create_code_change_mission(
        workflow,
        goal="Change VALUE from 1 to 2.",
        actor_id="operator",
        repository_root=repository,
        worktree_parent=worktrees,
        result_root=results,
        worker_command=[sys.executable, "-I", "-m", "agent_loop.code_change"],
        verifier_command=[sys.executable, "-I", "-m", "agent_loop.code_change_verify"],
        model_broker_socket=tmp_path / "model.sock",
        model_request=model_request,
        max_patch_bytes=16_000,
        worker_user=pwd.getpwuid(os.getuid()).pw_name,
    )

    assert created.mission.state == "active"
    assert tuple(created.tasks) == ("planner", "implementer", "reviewer", "verifier")
    assert [created.tasks[role].status for role in created.tasks] == [
        "ready",
        "blocked",
        "blocked",
        "blocked",
    ]
    assert [created.tasks[role].assignee for role in created.tasks] == [
        "change-planner",
        "change-implementer",
        "change-reviewer",
        "change-verifier",
    ]
    for role, task in created.tasks.items():
        assert task.specification["change_role"] == role
        assert task.specification["cwd"] == created.prepared.worktree_root
        assert task.specification["base_commit"] == created.prepared.base_commit
        assert task.specification["model_request"] == model_request
        assert task.max_attempts == 1
        assert task.resources == (f"worktree:{created.prepared.worktree_root}",)
    assert created.tasks["verifier"].acceptance["verification_command"] == [
        sys.executable,
        "-I",
        "-m",
        "agent_loop.code_change_verify",
    ]
    with workflow.store.read() as connection:
        dependencies = connection.execute(
            "SELECT parent_task_id, child_task_id FROM task_dependencies ORDER BY created_at"
        ).fetchall()
    assert [(row[0], row[1]) for row in dependencies] == [
        (created.tasks["planner"].task_id, created.tasks["implementer"].task_id),
        (created.tasks["implementer"].task_id, created.tasks["reviewer"].task_id),
        (created.tasks["reviewer"].task_id, created.tasks["verifier"].task_id),
    ]
    assert Path(created.verification_result_path).is_relative_to(results.resolve())
    assert (
        Path(created.prepared.worktree_root) / "service.py"
    ).stat().st_uid == os.getuid()
    assert json.loads(json.dumps(created.mission.limits))["max_patch_bytes"] == 16_000


class FakeControl:
    def __init__(self) -> None:
        self.artifacts: dict[str, bytes] = {}
        self.messages: list[dict[str, object]] = []

    def __call__(
        self,
        _socket: str,
        _token: str,
        method: str,
        params: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        values = params or {}
        if method == "heartbeat":
            return {"status": "running"}
        if method == "artifact.put":
            artifact_id = f"artifact-{len(self.artifacts) + 1}"
            self.artifacts[artifact_id] = __import__("base64").b64decode(
                str(values["content_base64"]), validate=True
            )
            return {"artifact_id": artifact_id}
        if method == "artifact.read":
            return {
                "content_base64": __import__("base64")
                .b64encode(self.artifacts[str(values["artifact_id"])])
                .decode("ascii")
            }
        if method == "message.publish":
            self.messages.append(values)
            return {"message_id": f"message-{len(self.messages)}"}
        raise AssertionError(method)


def result_artifact(
    control: FakeControl, artifact_ids: list[str], role: str
) -> dict[str, object]:
    for artifact_id in artifact_ids:
        try:
            value = json.loads(control.artifacts[artifact_id])
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict) and value.get("change_role") == role:
            return value
    raise AssertionError(f"missing {role} result")


def test_four_role_worker_pipeline_produces_verified_patch(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    results = tmp_path / "results"
    worktrees.mkdir()
    results.mkdir()
    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")
    verification_result = results / "verifier-result.json"
    source_digest = repository_digest(repository)
    control = FakeControl()
    responses = {
        "planner": {
            "summary": "Change the configured value.",
            "changes": [
                {"path": "service.py", "instruction": "Change VALUE from 1 to 2."}
            ],
        },
        "implementer": {
            "summary": "Changed the configured value.",
            "edits": [
                {
                    "path": "service.py",
                    "expected_sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest(),
                    "content": "VALUE = 2\n",
                }
            ],
        },
        "reviewer": {"verdict": "pass", "issues": []},
        "verifier": {"verdict": "pass", "issues": []},
    }

    def broker(
        _socket: str, payload: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        role = str(payload["request_id"]).rsplit(":", 1)[-1]
        return {
            "model": "local-model",
            "content": json.dumps(responses[role]),
            "finish_reason": "stop",
            "usage": {"total_tokens": 10},
        }

    common_specification = {
        "repository_root": prepared.repository_root,
        "worktree_root": prepared.worktree_root,
        "base_commit": prepared.base_commit,
        "source_digest": prepared.source_digest,
        "worktree_gitfile_sha256": prepared.worktree_gitfile_sha256,
        "model_broker_socket": str(tmp_path / "model.sock"),
        "model_request": {
            "model": "local-model",
            "max_tokens": 1_024,
            "max_prompt_chars": 16_000,
            "temperature": 0,
        },
        "max_patch_bytes": 16_000,
        "result_root": str(results),
        "verification_result_path": str(verification_result),
    }
    parent_artifacts: list[str] = []
    role_results: dict[str, dict[str, object]] = {}
    for role in ("planner", "implementer", "reviewer", "verifier"):
        request = {
            "mission_id": "mission-1",
            "task_id": f"task-{role}",
            "run_id": f"run-{role}",
            "worker_id": f"worker-{role}",
            "goal": "Change VALUE from 1 to 2.",
            "specification": {**common_specification, "change_role": role},
            "acceptance": {},
            "context": {
                "parent_handoffs": []
                if not parent_artifacts
                else [
                    {
                        "task_id": "parent",
                        "summary": "done",
                        "metadata": {"artifact_ids": parent_artifacts},
                    }
                ]
            },
            "workspace": prepared.worktree_root,
            "limits": {},
            "control": {
                "socket_path": str(tmp_path / "control.sock"),
                "token": "run-token",
            },
        }
        runner_result = run_code_change(request, broker=broker, control=control)
        assert runner_result["outcome"] == "candidate_complete"
        parent_artifacts = list(runner_result["artifact_ids"])
        role_results[role] = result_artifact(control, parent_artifacts, role)

    assert (Path(prepared.worktree_root) / "service.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 2\n"
    assert repository_digest(repository) == source_digest
    assert (
        role_results["implementer"]["patch_artifact_id"]
        == role_results["reviewer"]["patch_artifact_id"]
    )
    assert (
        role_results["reviewer"]["patch_artifact_id"]
        == role_results["verifier"]["source_patch_artifact_id"]
    )
    assert verification_result.is_file()
    verified = verify_code_change_result(
        {
            "specification": {**common_specification, "change_role": "verifier"},
            "workspace": prepared.worktree_root,
            "control": {},
        }
    )
    assert verified["verified"] is True
    assert verified["changed_paths"] == ["service.py"]
    assert verified["patch_sha256"] == role_results["verifier"]["patch_sha256"]
    assert len(control.messages) == 4


def test_reviewer_failure_stops_the_pipeline_candidate(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    prepared = prepare_code_change_worktree(repository, worktrees, "mission-1")
    control = FakeControl()
    patch = b"diff --git a/service.py b/service.py\n"
    patch_artifact = "artifact-1"
    implementer_result = {
        "change_role": "implementer",
        "patch_artifact_id": patch_artifact,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "changed_paths": ["service.py"],
    }
    control.artifacts[patch_artifact] = patch
    control.artifacts["artifact-2"] = json.dumps(implementer_result).encode()

    def broker(
        _socket: str, _payload: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        return {
            "model": "local-model",
            "content": '{"verdict":"fail","issues":["The change is wrong."]}',
            "finish_reason": "stop",
            "usage": {},
        }

    result = run_code_change(
        {
            "mission_id": "mission-1",
            "task_id": "task-reviewer",
            "run_id": "run-reviewer",
            "worker_id": "worker-reviewer",
            "goal": "Change VALUE.",
            "specification": {
                "change_role": "reviewer",
                "repository_root": prepared.repository_root,
                "worktree_root": prepared.worktree_root,
                "base_commit": prepared.base_commit,
                "source_digest": prepared.source_digest,
                "worktree_gitfile_sha256": prepared.worktree_gitfile_sha256,
                "model_broker_socket": str(tmp_path / "model.sock"),
                "model_request": {
                    "model": "local-model",
                    "max_tokens": 512,
                    "max_prompt_chars": 8_000,
                    "temperature": 0,
                },
                "max_patch_bytes": 16_000,
                "result_root": str(tmp_path),
                "verification_result_path": str(tmp_path / "verifier-result.json"),
            },
            "context": {
                "parent_handoffs": [
                    {
                        "task_id": "task-implementer",
                        "summary": "done",
                        "metadata": {"artifact_ids": [patch_artifact, "artifact-2"]},
                    }
                ]
            },
            "workspace": prepared.worktree_root,
            "control": {
                "socket_path": str(tmp_path / "control.sock"),
                "token": "run-token",
            },
        },
        broker=broker,
        control=control,
    )

    assert result["outcome"] == "failed"
    assert "reviewer rejected" in result["summary"]


def test_control_cli_creates_code_change_mission(tmp_path: Path, capsys) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    results = tmp_path / "results"
    worktrees.mkdir()
    results.mkdir()
    database = tmp_path / "control.db"

    code = control_main(
        [
            "--db",
            str(database),
            "code-change-create",
            "Change VALUE from 1 to 2.",
            "--actor",
            "operator",
            "--repository",
            str(repository),
            "--worktree-parent",
            str(worktrees),
            "--result-root",
            str(results),
            "--worker-command-json",
            json.dumps([sys.executable, "-I", "-m", "agent_loop.code_change"]),
            "--verifier-command-json",
            json.dumps([sys.executable, "-I", "-m", "agent_loop.code_change_verify"]),
            "--model-broker-socket",
            str(tmp_path / "model.sock"),
            "--model-request-json",
            '{"model":"local-model","max_tokens":1024,"max_prompt_chars":16000,"temperature":0}',
            "--worker-user",
            pwd.getpwuid(os.getuid()).pw_name,
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert captured.err == ""
    assert payload["mission"]["state"] == "active"
    assert list(payload["tasks"]) == ["implementer", "planner", "reviewer", "verifier"]
    assert Path(payload["prepared"]["worktree_root"]).is_dir()


def test_real_control_plane_runs_four_role_code_change_to_verified_patch(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    results = tmp_path / "results"
    artifacts_root = tmp_path / "artifacts"
    worktrees.mkdir()
    results.mkdir()
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    artifacts = ArtifactStore(store, artifacts_root)
    tokens = RunTokenService(store, workflow)
    board = MessageBoard(store)
    control_socket = tmp_path / "control.sock"
    model_socket = tmp_path / "model.sock"
    created = create_code_change_mission(
        workflow,
        goal="Change VALUE from 1 to 2.",
        actor_id="operator",
        repository_root=repository,
        worktree_parent=worktrees,
        result_root=results,
        worker_command=[sys.executable, "-I", "-m", "agent_loop.code_change"],
        verifier_command=[sys.executable, "-I", "-m", "agent_loop.code_change_verify"],
        model_broker_socket=model_socket,
        model_request={
            "model": "local-model",
            "max_tokens": 1_024,
            "max_prompt_chars": 16_000,
            "temperature": 0,
        },
        max_patch_bytes=16_000,
    )

    def provider(**payload: object) -> dict[str, object]:
        prompt = json.dumps(payload["messages"])
        if "Select one to four existing text files" in prompt:
            content = {
                "summary": "Change the configured value.",
                "changes": [
                    {"path": "service.py", "instruction": "Change VALUE from 1 to 2."}
                ],
            }
        elif "Return complete replacement content" in prompt:
            content = {
                "summary": "Changed the configured value.",
                "edits": [
                    {
                        "path": "service.py",
                        "expected_sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest(),
                        "content": "VALUE = 2\n",
                    }
                ],
            }
        else:
            content = {"verdict": "pass", "issues": []}
        return {
            "content": json.dumps(content),
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    api = WorkerAPI(
        tokens=tokens,
        workflow=workflow,
        board=board,
        artifacts=artifacts,
        action_broker=ActionBroker(store, risk_policy={}),
    )
    capabilities = {
        "run.heartbeat",
        "message.publish",
        "artifact.read",
        "artifact.write",
        "model.invoke",
    }
    roles = {
        f"change-{role}" for role in ("planner", "implementer", "reviewer", "verifier")
    }
    runner = JsonSubprocessRunner(
        allowed_commands=[sys.executable],
        allowed_workspace_roots=[tmp_path],
        cgroup_root=None,
    )
    coordinator = Coordinator(
        workflow,
        runner,
        worker_id="code-worker",
        roles=roles,
        lease_seconds=30,
        artifacts=artifacts,
        budgets=BudgetService(store),
        run_tokens=tokens,
        control_socket_path=control_socket,
        capabilities_by_role={role: capabilities for role in roles},
    )
    broker = ModelBroker(
        ModelBrokerPolicy(
            model="local-model",
            endpoint="http://127.0.0.1:19434",
            worker_uid=os.getuid(),
            control_socket_root=tmp_path,
            max_tokens=1_024,
            max_prompt_chars=16_000,
            timeout_seconds=30,
            max_concurrent=1,
        ),
        audit_log=tmp_path / "model-audit.jsonl",
        provider=provider,
    )

    with (
        ControlSocketServer(
            control_socket,
            api,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            mode=0o600,
        ),
        ModelBrokerSocketServer(model_socket, broker),
    ):
        coordinator_results = coordinator.run_until_idle(max_tasks=4)

    assert [result.status for result in coordinator_results] == ["completed"] * 4
    tasks = workflow.list_tasks(created.mission.mission_id)
    assert [task.status for task in tasks] == ["succeeded"] * 4
    assert (
        Path(created.prepared.worktree_root) / "service.py"
    ).read_text() == "VALUE = 2\n"
    assert (Path(created.verification_result_path).with_suffix(".patch")).is_file()
    assert len(board.list_messages(created.mission.mission_id)) == 4
