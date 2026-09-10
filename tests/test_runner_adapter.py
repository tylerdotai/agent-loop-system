from __future__ import annotations

import base64
import json
import os
import pwd
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_loop.action_broker import BudgetService
from agent_loop.artifacts import ArtifactStore
from agent_loop.coordinator import Coordinator
from agent_loop.message_board import MessageBoard
from agent_loop.persistence import SQLiteStore
from agent_loop.runner_adapter import (
    JsonSubprocessRunner,
    RunRequest,
    RunnerCancelledError,
    RunnerPolicyError,
    RunnerProtocolError,
    RunnerTimeoutError,
)
from agent_loop.workflow import WorkflowService
from agent_loop.worker_api import ControlSocketServer, RunTokenService, WorkerAPI


def make_request(tmp_path: Path) -> RunRequest:
    return RunRequest(
        mission_id="mission-1",
        task_id="task-1",
        run_id="run-1",
        worker_id="worker-1",
        goal="Produce verified output",
        specification={"format": "json"},
        acceptance={"required_evidence_kinds": ["command"]},
        context={"parent_handoffs": []},
        workspace=str(tmp_path),
        limits={"max_actions": 10},
    )


def valid_result(summary: str = "complete") -> dict:
    return {
        "outcome": "candidate_complete",
        "summary": summary,
        "artifact_ids": [],
        "evidence": [{"kind": "command", "value": "check", "exit_code": 0}],
        "fact_proposals": [],
        "residual_risks": [],
        "requested_followups": [],
    }


def test_json_runner_crosses_real_stdin_stdout_contract(tmp_path: Path) -> None:
    source = (
        "import json,sys; request=json.load(sys.stdin); "
        "print(json.dumps({"
        "'outcome':'candidate_complete','summary':request['goal'],"
        "'artifact_ids':[],'evidence':[{'kind':'command','value':'probe','exit_code':0}],"
        "'fact_proposals':[],'residual_risks':[],'requested_followups':[]"
        "}))"
    )
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    result = runner.run(make_request(tmp_path), [sys.executable, "-c", source], timeout_seconds=5)

    assert result.outcome == "candidate_complete"
    assert result.summary == "Produce verified output"
    assert result.evidence[0]["kind"] == "command"


def test_runner_rejects_malformed_or_weak_completion_contract(tmp_path: Path) -> None:
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    with pytest.raises(RunnerProtocolError, match="valid JSON object"):
        runner.run(make_request(tmp_path), [sys.executable, "-c", "print('not-json')"], timeout_seconds=5)

    weak = json.dumps({"outcome": "candidate_complete", "summary": "trust me"})
    with pytest.raises(RunnerProtocolError, match="artifact_ids"):
        runner.run(make_request(tmp_path), [sys.executable, "-c", f"print({weak!r})"], timeout_seconds=5)


def test_runner_rejects_unallowlisted_executable_before_start(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    with pytest.raises(RunnerPolicyError, match="not allowed"):
        runner.run(make_request(tmp_path), ["sh", "-c", f"touch {marker}"], timeout_seconds=5)

    assert not marker.exists()


def test_timeout_terminates_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    child_source = f"import time,pathlib; time.sleep(0.4); pathlib.Path({str(marker)!r}).write_text('bad')"
    parent_source = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_source!r}]); "
        "time.sleep(10)"
    )
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    with pytest.raises(RunnerTimeoutError, match="timed out"):
        runner.run(make_request(tmp_path), [sys.executable, "-c", parent_source], timeout_seconds=0.1)

    time.sleep(0.6)
    assert not marker.exists()


def test_cancellation_callback_terminates_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "cancelled-child-survived"
    child_source = f"import time,pathlib; time.sleep(0.4); pathlib.Path({str(marker)!r}).write_text('bad')"
    parent_source = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_source!r}]); "
        "time.sleep(10)"
    )
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})
    cancel_at = time.monotonic() + 0.1

    with pytest.raises(RunnerCancelledError, match="cancelled"):
        runner.run(
            make_request(tmp_path),
            [sys.executable, "-c", parent_source],
            timeout_seconds=5,
            cancel_requested=lambda: time.monotonic() >= cancel_at,
        )

    time.sleep(0.6)
    assert not marker.exists()


def test_timeout_kills_child_that_ignores_sigterm_after_parent_exits(tmp_path: Path) -> None:
    marker = tmp_path / "sigterm-ignoring-child-survived"
    child_source = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    parent_source = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_source!r}]); "
        "time.sleep(10)"
    )
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    with pytest.raises(RunnerTimeoutError, match="timed out"):
        runner.run(make_request(tmp_path), [sys.executable, "-c", parent_source], timeout_seconds=0.15)

    time.sleep(0.7)
    assert not marker.exists()


def test_timeout_kills_descendant_that_created_a_new_session(tmp_path: Path) -> None:
    marker = tmp_path / "detached-child-survived"
    child_source = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    parent_source = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_source!r}], start_new_session=True); "
        "time.sleep(10)"
    )
    runner = JsonSubprocessRunner(
        allowed_commands={Path(sys.executable).name},
        cgroup_root=None,
    )

    with pytest.raises(RunnerTimeoutError, match="timed out"):
        runner.run(make_request(tmp_path), [sys.executable, "-c", parent_source], timeout_seconds=0.15)

    time.sleep(0.7)
    assert not marker.exists()


def test_successful_parent_cannot_leave_daemonized_child_in_run_cgroup(tmp_path: Path) -> None:
    marker = tmp_path / "successful-parent-detached-child-survived"
    child_source = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    payload = json.dumps(valid_result("parent returned"))
    parent_source = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{child_source!r}], start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"print({payload!r})"
    )
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})
    if runner.cgroup_root is None:
        pytest.skip("delegated cgroup v2 is unavailable")

    result = runner.run(
        make_request(tmp_path),
        [sys.executable, "-c", parent_source],
        timeout_seconds=5,
    )

    assert result.outcome == "candidate_complete"
    time.sleep(0.7)
    assert not marker.exists()


def test_successful_parent_cannot_leave_daemonized_child_without_cgroup(tmp_path: Path) -> None:
    marker = tmp_path / "fallback-success-detached-child-survived"
    child_source = (
        "import pathlib,time; "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    payload = json.dumps(valid_result("parent returned without cgroup"))
    parent_source = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{child_source!r}], start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"print({payload!r})"
    )
    runner = JsonSubprocessRunner(
        allowed_commands={Path(sys.executable).name},
        cgroup_root=None,
    )

    result = runner.run(
        make_request(tmp_path),
        [sys.executable, "-c", parent_source],
        timeout_seconds=5,
    )

    assert result.outcome == "candidate_complete"
    time.sleep(0.7)
    assert not marker.exists()


def test_runner_redacts_sensitive_failure_text_and_caps_tail(tmp_path: Path) -> None:
    secret = "example-sensitive-value"
    source = f"import sys; sys.stderr.write('prefix-' + 'x'*200 + '-{secret}'); raise SystemExit(7)"
    runner = JsonSubprocessRunner(
        allowed_commands={Path(sys.executable).name},
        redact_values={secret},
        max_output_chars=80,
    )

    with pytest.raises(RunnerProtocolError) as error:
        runner.run(make_request(tmp_path), [sys.executable, "-c", source], timeout_seconds=5)

    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert len(str(error.value)) < 180


def test_runner_never_leaks_dynamic_control_token_when_child_echoes_request(tmp_path: Path) -> None:
    token = "one-run-secret-token"
    original = make_request(tmp_path)
    request = RunRequest(
        **{**original.__dict__, "control": {"socket_path": "/tmp/control.sock", "token": token}}
    )
    source = "import sys; payload=sys.stdin.read(); sys.stderr.write(payload); raise SystemExit(9)"
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    with pytest.raises(RunnerProtocolError) as error:
        runner.run(request, [sys.executable, "-c", source], timeout_seconds=5)

    assert token not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_runner_scrubs_common_encodings_and_long_fragments_of_run_token(tmp_path: Path) -> None:
    token = "scoped-run-token-with-enough-random-material-123456789"
    encoded = base64.b64encode(token.encode()).decode()
    hexadecimal = token.encode().hex()
    midpoint = len(token) // 2
    original = make_request(tmp_path)
    request = RunRequest(
        **{**original.__dict__, "control": {"socket_path": "/tmp/control.sock", "token": token}}
    )
    result_payload = valid_result(encoded)
    result_payload["residual_risks"] = [hexadecimal, token[:midpoint], token[midpoint:]]
    source = f"print({json.dumps(result_payload)!r})"
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    result = runner.run(request, [sys.executable, "-c", source], timeout_seconds=5)

    rendered = json.dumps(result.metadata())
    assert encoded not in rendered
    assert hexadecimal not in rendered
    assert token[:midpoint] not in rendered
    assert token[midpoint:] not in rendered
    assert "[REDACTED]" in rendered


def test_runner_timeout_must_be_finite(tmp_path: Path) -> None:
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    for value in (float("nan"), float("inf")):
        with pytest.raises(RunnerPolicyError, match="finite"):
            runner.run(
                make_request(tmp_path),
                [sys.executable, "-c", "raise AssertionError('must not start')"],
                timeout_seconds=value,
            )


def test_coordinator_completes_only_after_required_evidence(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Run one task", "operator", state="active")
    payload = json.dumps(valid_result("verified candidate"))
    task = workflow.create_task(
        mission.mission_id,
        "Generate candidate",
        "builder",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", f"print({payload!r})"], "timeout_seconds": 5},
        acceptance={"required_evidence_kinds": ["command"]},
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="builder-1",
        roles={"builder"},
    )

    result = coordinator.run_once()

    assert result is not None
    assert result.status == "completed"
    completed = workflow.get_task(task.task_id)
    assert completed.status == "succeeded"
    assert completed.completion_summary == "verified candidate"
    assert completed.completion_metadata["evidence"][0]["kind"] == "command"


def test_default_task_workspace_is_created_under_first_admitted_root(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state" / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Default workspace", "operator", state="active")
    payload = json.dumps(valid_result("workspace admitted"))
    task = workflow.create_task(
        mission.mission_id,
        "Run in default workspace",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", f"print({payload!r})"]},
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(
            allowed_commands={Path(sys.executable).name},
            allowed_workspace_roots={workspace_root},
        ),
        worker_id="worker-1",
        roles={"worker"},
    )

    result = coordinator.run_once()

    assert result is not None
    assert result.status == "completed"
    assert (workspace_root / mission.mission_id / task.task_id).is_dir()


def test_coordinator_rejects_candidate_missing_required_evidence(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Reject weak task", "operator", state="active")
    weak = valid_result("weak candidate")
    weak["evidence"] = []
    task = workflow.create_task(
        mission.mission_id,
        "Weak candidate",
        "builder",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", f"print({json.dumps(weak)!r})"]},
        acceptance={"required_evidence_kinds": ["command"]},
        max_attempts=1,
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="builder-1",
        roles={"builder"},
    )

    result = coordinator.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "required evidence" in (result.error or "")
    assert workflow.get_task(task.task_id).status == "failed"
    assert workflow.list_runs(task.task_id)[0].outcome == "validation_failed"


def test_coordinator_preserves_redacted_worker_failure_summary(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Diagnose failure", "operator", state="active")
    failed = valid_result("model broker socket was unavailable")
    failed["outcome"] = "failed"
    task = workflow.create_task(
        mission.mission_id,
        "Fail with bounded detail",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", f"print({json.dumps(failed)!r})"]},
        max_attempts=1,
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
    )

    result = coordinator.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "model broker socket was unavailable" in (result.error or "")
    assert "model broker socket was unavailable" in (
        workflow.list_runs(task.task_id)[0].error or ""
    )


def test_supervisor_verification_command_must_pass_before_completion(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Verify independently", "operator", state="active")
    marker = tmp_path / "verified"
    worker_payload = json.dumps(valid_result("candidate"))
    task = workflow.create_task(
        mission.mission_id,
        "Run independent check",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", f"print({worker_payload!r})"], "cwd": str(tmp_path)},
        acceptance={
            "required_evidence_kinds": ["command"],
            "verification_command": [
                sys.executable,
                "-c",
                f"import pathlib; pathlib.Path({str(marker)!r}).write_text('verified')",
            ],
            "verification_timeout_seconds": 5,
        },
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
    )

    result = coordinator.run_once()

    assert result is not None
    assert result.status == "completed"
    assert marker.read_text() == "verified"
    metadata = workflow.get_task(task.task_id).completion_metadata
    assert metadata["verification"]["exit_code"] == 0
    assert metadata["verification"]["source"] == "control_plane"


def test_failed_supervisor_verification_rejects_worker_claimed_evidence(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Reject fabricated evidence", "operator", state="active")
    worker_payload = json.dumps(valid_result("unverified claim"))
    task = workflow.create_task(
        mission.mission_id,
        "Fail independent check",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", f"print({worker_payload!r})"], "cwd": str(tmp_path)},
        acceptance={
            "required_evidence_kinds": ["command"],
            "verification_command": [sys.executable, "-c", "raise SystemExit(7)"],
            "verification_timeout_seconds": 5,
        },
        max_attempts=1,
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
    )

    result = coordinator.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "verification command failed (7)" in (result.error or "")
    assert workflow.get_task(task.task_id).status == "failed"
    assert workflow.list_runs(task.task_id)[0].outcome == "verification_failed"


def test_coordinator_rejects_unknown_artifact_ids(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Verify artifacts", "operator", state="active")
    result_payload = valid_result("claims fake artifact")
    result_payload["artifact_ids"] = ["art_does_not_exist"]
    source = f"print({json.dumps(result_payload)!r})"
    workflow.create_task(
        mission.mission_id,
        "Require artifact",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", source], "cwd": str(tmp_path)},
        acceptance={"minimum_artifacts": 1},
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
        artifacts=ArtifactStore(store, tmp_path / "artifacts"),
    )

    outcome = coordinator.run_once()

    assert outcome is not None
    assert outcome.status == "failed"
    assert "unknown artifact" in (outcome.error or "")


def test_coordinator_drains_parent_then_promoted_child(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Pipeline", "operator", state="active")
    command = [sys.executable, "-c", f"print({json.dumps(valid_result())!r})"]
    parent = workflow.create_task(
        mission.mission_id,
        "Parent",
        "worker",
        actor_id="planner",
        specification={"command": command},
        acceptance={"required_evidence_kinds": ["command"]},
    )
    child = workflow.create_task(
        mission.mission_id,
        "Child",
        "worker",
        actor_id="planner",
        parents=(parent.task_id,),
        specification={"command": command},
        acceptance={"required_evidence_kinds": ["command"]},
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
    )

    results = coordinator.run_until_idle(max_tasks=3)

    assert [result.status for result in results] == ["completed", "completed"]
    assert workflow.get_task(parent.task_id).status == "succeeded"
    assert workflow.get_task(child.task_id).status == "succeeded"
    assert workflow.build_task_context(child.task_id)["parent_handoffs"][0]["task_id"] == parent.task_id


def test_runner_environment_is_explicit_and_does_not_accept_non_string_values(tmp_path: Path) -> None:
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})
    source = (
        "import json,os,sys; json.load(sys.stdin); "
        "value=os.environ.get('RUNNER_TEST_VALUE','missing'); "
        "print(json.dumps({'outcome':'candidate_complete','summary':value,"
        "'artifact_ids':[],'evidence':[{'kind':'command','value':'check','exit_code':0}],"
        "'fact_proposals':[],'residual_risks':[],'requested_followups':[]}))"
    )
    result = runner.run(
        make_request(tmp_path),
        [sys.executable, "-c", source],
        timeout_seconds=5,
        env={"RUNNER_TEST_VALUE": "present"},
    )
    assert result.summary == "present"

    with pytest.raises(RunnerPolicyError, match="string keys and values"):
        runner.run(
            make_request(tmp_path),
            [sys.executable, "-c", source],
            timeout_seconds=5,
            env={"BAD": 1},  # type: ignore[dict-item]
        )


def test_coordinator_issues_scoped_socket_token_and_revokes_it_after_run(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Communicate through scoped API", "operator", state="active")
    socket_path = tmp_path / "control.sock"
    source = """
import json
import socket
import sys

request = json.load(sys.stdin)
control = request["control"]
message = {
    "token": control["token"],
    "method": "message.publish",
    "params": {
        "topic": f"mission.{request['mission_id']}.general",
        "kind": "checkpoint",
        "body": "child reached the control socket",
        "dedupe_key": f"{request['run_id']}:checkpoint:1",
    },
}
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect(control["socket_path"])
    client.sendall(json.dumps(message).encode() + b"\\n")
    response = b""
    while not response.endswith(b"\\n"):
        response += client.recv(65536)
assert json.loads(response)["ok"] is True
print(json.dumps({
    "outcome": "candidate_complete",
    "summary": "scoped API used",
    "artifact_ids": [],
    "evidence": [{"kind": "command", "value": "socket round trip", "exit_code": 0}],
    "fact_proposals": [],
    "residual_risks": [],
    "requested_followups": [],
}))
"""
    workflow.create_task(
        mission.mission_id,
        "Use worker API",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", source], "cwd": str(tmp_path)},
        acceptance={"required_evidence_kinds": ["command"]},
    )
    tokens = RunTokenService(store, workflow)
    board = MessageBoard(store)
    api = WorkerAPI(
        tokens=tokens,
        workflow=workflow,
        board=board,
        artifacts=ArtifactStore(store, tmp_path / "artifacts"),
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
        run_tokens=tokens,
        control_socket_path=socket_path,
        capabilities_by_role={"worker": {"message.publish"}},
    )

    with ControlSocketServer(socket_path, api):
        result = coordinator.run_once()

    assert result is not None
    assert result.status == "completed"
    messages = board.list_messages(mission.mission_id)
    assert len(messages) == 1
    assert messages[0].actor_id == "worker-1"
    with store.read() as conn:
        token_row = conn.execute("SELECT revoked_at FROM run_tokens").fetchone()
    assert token_row is not None
    assert token_row["revoked_at"] is not None


def test_coordinator_daemon_polls_without_busy_spinning_when_idle(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
    )
    sleeps: list[float] = []

    processed = coordinator.run_daemon(
        poll_seconds=0.25,
        max_cycles=3,
        sleep=sleeps.append,
    )

    assert processed == 0
    assert sleeps == [0.25, 0.25]


def test_mission_cancellation_stops_the_live_worker_process(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Stop live work", "operator", state="active")
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    source = (
        "import pathlib,time; "
        f"pathlib.Path({str(started)!r}).write_text('started'); "
        "time.sleep(10); "
        f"pathlib.Path({str(survived)!r}).write_text('bad')"
    )
    workflow.create_task(
        mission.mission_id,
        "Long worker",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", source], "cwd": str(tmp_path)},
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
    )
    outcomes: list = []
    thread = threading.Thread(target=lambda: outcomes.append(coordinator.run_once()))
    thread.start()
    for _ in range(100):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists()

    workflow.cancel_mission(mission.mission_id, "operator", reason="stop now")
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert outcomes[0] is not None
    assert outcomes[0].status == "failed"
    assert "cancelled" in (outcomes[0].error or "")
    time.sleep(0.5)
    assert not survived.exists()


def test_coordinator_heartbeats_healthy_process_without_model_cooperation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Infrastructure heartbeat", "operator", state="active")
    started = tmp_path / "heartbeat-started"
    release = tmp_path / "heartbeat-release"
    heartbeat_seen = threading.Event()
    heartbeat_calls = 0
    original_heartbeat = workflow.heartbeat

    def recording_heartbeat(*args, **kwargs):
        nonlocal heartbeat_calls
        result = original_heartbeat(*args, **kwargs)
        heartbeat_calls += 1
        if started.exists() and heartbeat_calls >= 2:
            heartbeat_seen.set()
        return result

    monkeypatch.setattr(workflow, "heartbeat", recording_heartbeat)
    payload = json.dumps(valid_result("healthy long task"))
    source = "\n".join(
        (
            "import pathlib,time",
            f"pathlib.Path({str(started)!r}).write_text('started')",
            "deadline=time.time()+5",
            f"while not pathlib.Path({str(release)!r}).exists() and time.time()<deadline:",
            "    time.sleep(0.02)",
            f"print({payload!r})",
        )
    )
    workflow.create_task(
        mission.mission_id,
        "Long healthy worker",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", source], "cwd": str(tmp_path)},
        max_runtime_seconds=6,
    )
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
        lease_seconds=2,
    )
    outcomes: list = []
    thread = threading.Thread(target=lambda: outcomes.append(coordinator.run_once()))
    thread.start()
    for _ in range(200):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists()
    assert heartbeat_seen.wait(timeout=3)

    recovered = workflow.recover_expired("watchdog")
    release.write_text("continue", encoding="utf-8")
    thread.join(timeout=3)

    assert recovered == []
    assert not thread.is_alive()
    assert outcomes[0] is not None
    assert outcomes[0].status == "completed", outcomes[0]
    assert heartbeat_calls >= 2


def test_exhausted_mission_run_budget_prevents_worker_process_start(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Budgeted work", "operator", state="active")
    marker = tmp_path / "must-not-start"
    payload = json.dumps(valid_result("should not execute"))
    source = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('bad'); print({payload!r})"
    task = workflow.create_task(
        mission.mission_id,
        "Budget blocked",
        "worker",
        actor_id="planner",
        specification={"command": [sys.executable, "-c", source], "cwd": str(tmp_path)},
        max_attempts=1,
    )
    budgets = BudgetService(store)
    budgets.set_limit("mission", mission.mission_id, "runs", 1, actor_id="operator")
    budgets.reserve("mission", mission.mission_id, "runs", 1, "already-used")
    budgets.consume("already-used")
    coordinator = Coordinator(
        workflow,
        JsonSubprocessRunner(allowed_commands={Path(sys.executable).name}),
        worker_id="worker-1",
        roles={"worker"},
        budgets=budgets,
    )

    result = coordinator.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "budget exceeded" in (result.error or "")
    assert workflow.get_task(task.task_id).status == "failed"
    assert not marker.exists()


def test_runner_does_not_inherit_unspecified_supervisor_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERVISOR_PRIVATE_VALUE", "must-not-cross")
    monkeypatch.setenv("PATH", "/supervisor/private/bin")
    monkeypatch.setenv("LANG", "supervisor_LANG")
    monkeypatch.setenv("LC_ALL", "supervisor_LC_ALL")
    monkeypatch.setenv("LC_CTYPE", "supervisor_LC_CTYPE")
    monkeypatch.setenv("TZ", "supervisor_TZ")
    source = (
        "import json,os,sys; json.load(sys.stdin); "
        "values=[os.environ.get('SUPERVISOR_PRIVATE_VALUE','missing'),"
        "os.environ['PATH'],os.environ['LANG'],os.environ['LC_ALL'],"
        "os.environ['LC_CTYPE'],os.environ['TZ'],os.environ['HOME']]; "
        "print(json.dumps({'outcome':'candidate_complete',"
        "'summary':'|'.join(values),"
        "'artifact_ids':[],'evidence':[],'fact_proposals':[],"
        "'residual_risks':[],'requested_followups':[]}))"
    )
    runner = JsonSubprocessRunner(allowed_commands={Path(sys.executable).name})

    result = runner.run(make_request(tmp_path), [sys.executable, "-c", source], timeout_seconds=5)

    assert result.summary == (
        f"missing|/usr/local/bin:/usr/bin:/bin|C|C|C|UTC|{tmp_path}"
    )


def test_runner_builds_trusted_privilege_drop_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    class Account:
        pw_uid = 23456
        pw_gid = 23457
        pw_name = "agent-loop-worker"

    monkeypatch.setattr("agent_loop.runner_adapter.pwd.getpwnam", lambda _: Account())
    monkeypatch.setattr("agent_loop.runner_adapter.shutil.which", lambda _: "/usr/bin/setpriv")
    runner = JsonSubprocessRunner(
        allowed_commands={"python"},
        run_as_user="agent-loop-worker",
    )

    assert runner.worker_uid == 23456
    assert runner.worker_gid == 23457
    assert runner._launch_command(("python", "worker.py")) == (
        sys.executable,
        "-I",
        "-m",
        "agent_loop.sandbox_exec",
        "--",
        "/usr/bin/setpriv",
        "--reuid=23456",
        "--regid=23457",
        "--clear-groups",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--pdeathsig=SIGKILL",
        "--",
        "python",
        "worker.py",
    )


def test_default_workspace_leaf_is_handed_to_worker_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Account:
        pw_uid = 23456
        pw_gid = 23457
        pw_name = "agent-loop-worker"

    root = tmp_path / "workspaces"
    root.mkdir(mode=0o711)
    monkeypatch.setattr("agent_loop.runner_adapter.pwd.getpwnam", lambda _: Account())
    monkeypatch.setattr("agent_loop.runner_adapter.shutil.which", lambda _: "/usr/bin/setpriv")
    ownership: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        "agent_loop.runner_adapter.os.chown",
        lambda path, uid, gid: ownership.append((Path(path), uid, gid)),
    )
    runner = JsonSubprocessRunner(
        allowed_commands={"python"},
        allowed_workspace_roots={root},
        run_as_user="agent-loop-worker",
    )

    workspace = runner.prepare_workspace(root / "mission" / "task")

    assert workspace.stat().st_mode & 0o777 == 0o710
    assert workspace.parent.stat().st_mode & 0o777 == 0o711
    assert ownership == [
        (workspace, os.geteuid(), os.getegid()),
        (workspace, 23456, os.getegid()),
    ]
    assert os.getuid() >= 0


@pytest.mark.skipif(os.geteuid() != 0, reason="real UID isolation requires root")
def test_privilege_separated_worker_cannot_read_control_database(tmp_path: Path) -> None:
    account = pwd.getpwnam("nobody")
    tmp_path.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    database = state / "control.db"
    SQLiteStore(database)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o711)
    runner = JsonSubprocessRunner(
        allowed_commands={"python3"},
        allowed_workspace_roots={workspace_root},
        run_as_user=account.pw_name,
    )
    workspace = runner.prepare_workspace(workspace_root / "mission" / "task")
    request = RunRequest(
        mission_id="mission",
        task_id="task",
        run_id="run",
        worker_id="worker",
        goal="Prove DAC isolation",
        specification={},
        acceptance={},
        context={},
        workspace=str(workspace),
    )
    source = (
        "import json,pathlib,sys; json.load(sys.stdin); "
        f"path=pathlib.Path({str(database)!r}); "
        "denied=False; "
        "\ntry: path.read_bytes()\nexcept PermissionError: denied=True\n"
        "print(json.dumps({'outcome':'candidate_complete','summary':'denied' if denied else 'exposed',"
        "'artifact_ids':[],'evidence':[],'fact_proposals':[],'residual_risks':[],"
        "'requested_followups':[]}))"
    )

    result = runner.run(request, ["/usr/bin/python3", "-c", source], timeout_seconds=5)

    assert result.summary == "denied"
