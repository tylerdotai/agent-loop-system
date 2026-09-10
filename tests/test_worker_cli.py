from __future__ import annotations

import json
from pathlib import Path

from agent_loop.action_broker import ActionBroker
from agent_loop.artifacts import ArtifactStore
from agent_loop.message_board import MessageBoard
from agent_loop.persistence import SQLiteStore
from agent_loop.worker_api import ControlSocketServer, RunTokenService, WorkerAPI
from agent_loop.worker_cli import build_parser, main
from agent_loop.workflow import WorkflowService


def build_live_worker(tmp_path: Path, capabilities: set[str], *, broker: ActionBroker | None = None):
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Worker CLI", "operator", state="active")
    task = workflow.create_task(mission.mission_id, "Use CLI", "worker", actor_id="planner")
    claim = workflow.claim_next("worker-1", {"worker"})
    assert claim is not None
    tokens = RunTokenService(store, workflow)
    issued = tokens.issue(claim.run.run_id, capabilities, expires_in_seconds=60)
    board = MessageBoard(store)
    api = WorkerAPI(
        tokens=tokens,
        workflow=workflow,
        board=board,
        artifacts=ArtifactStore(store, tmp_path / "artifacts"),
        action_broker=broker,
    )
    return store, workflow, mission, task, issued, board, api


def test_worker_cli_uses_environment_identity_to_post_and_read_message(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _, _, mission, task, issued, board, api = build_live_worker(
        tmp_path,
        {"message.publish", "message.read"},
    )
    socket_path = tmp_path / "control.sock"
    monkeypatch.setenv("AGENT_LOOP_SOCKET", str(socket_path))
    monkeypatch.setenv("AGENT_LOOP_TOKEN", issued.token)
    topic = f"mission.{mission.mission_id}.general"

    with ControlSocketServer(socket_path, api):
        code = main(["message-post", topic, "checkpoint", "worker CLI reached board"])
        posted = json.loads(capsys.readouterr().out)
        listed_code = main(["message-list", "--topic-prefix", topic])
        listed = json.loads(capsys.readouterr().out)

    assert code == 0
    assert listed_code == 0
    assert posted["actor_id"] == "worker-1"
    assert posted["task_id"] == task.task_id
    assert listed[0]["message_id"] == posted["message_id"]
    assert board.list_messages(mission.mission_id)[0].body == "worker CLI reached board"


def test_worker_cli_uploads_and_downloads_artifact_without_overwriting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _, _, _, _, issued, _, api = build_live_worker(
        tmp_path,
        {"artifact.write", "artifact.read"},
    )
    socket_path = tmp_path / "control.sock"
    source = tmp_path / "source.txt"
    source.write_text("verified artifact", encoding="utf-8")
    output = tmp_path / "downloaded.txt"
    monkeypatch.setenv("AGENT_LOOP_SOCKET", str(socket_path))
    monkeypatch.setenv("AGENT_LOOP_TOKEN", issued.token)

    with ControlSocketServer(socket_path, api):
        assert main(["artifact-put", str(source), "--dedupe-key", "artifact:v1"]) == 0
        artifact = json.loads(capsys.readouterr().out)
        assert main(["artifact-read", artifact["artifact_id"], str(output)]) == 0
        downloaded = json.loads(capsys.readouterr().out)
        output.write_text("operator file", encoding="utf-8")
        assert main(["artifact-read", artifact["artifact_id"], str(output)]) == 2
        error = capsys.readouterr()

    assert output.read_text(encoding="utf-8") == "operator file"
    assert downloaded["bytes"] == len(b"verified artifact")
    assert "refusing to overwrite" in error.err


def test_worker_cli_missing_credentials_fails_cleanly(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AGENT_LOOP_SOCKET", raising=False)
    monkeypatch.delenv("AGENT_LOOP_TOKEN", raising=False)

    code = main(["context-get"])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert "AGENT_LOOP_SOCKET" in captured.err
    assert "Traceback" not in captured.err


def test_worker_cli_never_exposes_approval_or_execution_commands() -> None:
    help_text = build_parser().format_help()

    assert "action-propose" in help_text
    assert "action-approve" not in help_text
    assert "action-execute" not in help_text
    assert "--token" not in help_text
