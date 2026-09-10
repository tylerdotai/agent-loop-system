from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from agent_loop.persistence import SQLiteStore
from agent_loop.workflow import (
    DependencyCycleError,
    LeaseError,
    MissionStateError,
    WorkflowError,
    WorkflowService,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_service(tmp_path: Path, clock: FakeClock | None = None) -> WorkflowService:
    return WorkflowService(SQLiteStore(tmp_path / "control.db"), clock=clock or FakeClock())


def active_mission(service: WorkflowService, *, key: str | None = None):
    return service.create_mission("Build a verified artifact", "operator", state="active", idempotency_key=key)


def _claim_in_process(database: str, output: Any) -> None:
    service = WorkflowService(SQLiteStore(database))
    claim = service.claim_next("worker", {"coder"}, lease_seconds=60)
    output.put(None if claim is None else claim.task.task_id)


def test_mission_creation_is_idempotent_and_activation_is_explicit(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.create_mission("Research the topic", "operator", idempotency_key="daily:2026-09-09")
    duplicate = service.create_mission("Research the topic", "operator", idempotency_key="daily:2026-09-09")

    assert duplicate == first
    assert first.state == "draft"

    active = service.activate_mission(first.mission_id, "operator")
    assert active.state == "active"

    with pytest.raises(MissionStateError, match="cannot activate"):
        service.activate_mission(first.mission_id, "operator")


def test_dependencies_promote_child_only_after_every_parent_succeeds(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)
    first = service.create_task(mission.mission_id, "Research A", "researcher", actor_id="planner")
    second = service.create_task(mission.mission_id, "Research B", "researcher", actor_id="planner")
    child = service.create_task(
        mission.mission_id,
        "Synthesize",
        "writer",
        actor_id="planner",
        parents=(first.task_id, second.task_id),
    )

    assert child.status == "blocked"

    claim_a = service.claim_next("researcher-a", {"researcher"})
    assert claim_a is not None
    service.complete(claim_a.task.task_id, claim_a.run.run_id, "researcher-a", "A done")
    assert service.get_task(child.task_id).status == "blocked"

    claim_b = service.claim_next("researcher-b", {"researcher"})
    assert claim_b is not None
    service.complete(claim_b.task.task_id, claim_b.run.run_id, "researcher-b", "B done")
    assert service.get_task(child.task_id).status == "ready"


def test_atomic_claim_has_exactly_one_winner_across_processes(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    service = WorkflowService(SQLiteStore(database))
    mission = service.create_mission("Race test", "operator", state="active")
    task = service.create_task(mission.mission_id, "Only once", "coder", actor_id="planner")
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [context.Process(target=_claim_in_process, args=(str(database), output)) for _ in range(12)]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    results = [output.get(timeout=2) for _ in processes]
    assert results.count(task.task_id) == 1
    assert results.count(None) == len(processes) - 1


def test_exclusive_resource_lease_serializes_conflicting_tasks(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)
    first = service.create_task(
        mission.mission_id,
        "Edit API",
        "coder",
        actor_id="planner",
        resources=("file:src/api.py",),
        priority=2,
    )
    second = service.create_task(
        mission.mission_id,
        "Edit API tests",
        "coder",
        actor_id="planner",
        resources=("file:src/api.py",),
        priority=1,
    )

    first_claim = service.claim_next("coder-a", {"coder"}, lease_seconds=30)
    assert first_claim is not None
    assert first_claim.task.task_id == first.task_id
    assert service.claim_next("coder-b", {"coder"}, lease_seconds=30) is None

    service.complete(first.task_id, first_claim.run.run_id, "coder-a", "first complete")
    second_claim = service.claim_next("coder-b", {"coder"}, lease_seconds=30)
    assert second_claim is not None
    assert second_claim.task.task_id == second.task_id


def test_heartbeat_extends_only_the_current_owner_lease(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = active_mission(service)
    service.create_task(mission.mission_id, "Long task", "coder", actor_id="planner")
    claim = service.claim_next("coder-a", {"coder"}, lease_seconds=10)
    assert claim is not None
    assert claim.task.lease_expires_at == 110.0

    clock.value = 105.0
    renewed = service.heartbeat(claim.task.task_id, claim.run.run_id, "coder-a", lease_seconds=20)
    assert renewed.lease_expires_at == 125.0

    with pytest.raises(LeaseError, match="owned by another worker"):
        service.heartbeat(claim.task.task_id, claim.run.run_id, "coder-b", lease_seconds=20)


def test_expired_lease_requeues_then_exhausts_bounded_attempts(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = active_mission(service)
    task = service.create_task(
        mission.mission_id,
        "Retry twice",
        "coder",
        actor_id="planner",
        max_attempts=2,
    )

    first = service.claim_next("coder-a", {"coder"}, lease_seconds=10)
    assert first is not None
    clock.value = 111.0
    assert service.recover_expired("watchdog") == [task.task_id]
    assert service.get_task(task.task_id).status == "ready"

    second = service.claim_next("coder-b", {"coder"}, lease_seconds=10)
    assert second is not None
    clock.value = 122.0
    assert service.recover_expired("watchdog") == [task.task_id]
    recovered = service.get_task(task.task_id)
    assert recovered.status == "failed"
    assert recovered.attempts == 2
    assert [run.outcome for run in service.list_runs(task.task_id)] == ["expired", "expired"]


def test_link_rejects_dependency_cycle(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)
    first = service.create_task(mission.mission_id, "First", "coder", actor_id="planner")
    second = service.create_task(mission.mission_id, "Second", "coder", actor_id="planner")

    service.link_dependency(first.task_id, second.task_id, "planner")

    with pytest.raises(DependencyCycleError, match="cycle"):
        service.link_dependency(second.task_id, first.task_id, "planner")


def test_paused_mission_cannot_dispatch_or_accept_new_tasks(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)
    service.create_task(mission.mission_id, "Queued", "coder", actor_id="planner")

    paused = service.pause_mission(mission.mission_id, "operator", reason="operator review")

    assert paused.state == "paused"
    assert service.claim_next("coder-a", {"coder"}) is None
    with pytest.raises(MissionStateError, match="not active"):
        service.create_task(mission.mission_id, "Late task", "coder", actor_id="planner")


def test_parent_handoff_is_structured_context_not_transcript_scraping(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)
    parent = service.create_task(mission.mission_id, "Design", "architect", actor_id="planner")
    child = service.create_task(
        mission.mission_id,
        "Build",
        "coder",
        actor_id="planner",
        parents=(parent.task_id,),
    )
    claim = service.claim_next("architect-1", {"architect"})
    assert claim is not None
    service.complete(
        parent.task_id,
        claim.run.run_id,
        "architect-1",
        "Schema frozen",
        metadata={"artifact_ids": ["artifact-1"], "decisions": ["JSON Lines"]},
    )

    context = service.build_task_context(child.task_id)

    assert context["task"]["status"] == "ready"
    assert context["parent_handoffs"] == [
        {
            "task_id": parent.task_id,
            "summary": "Schema frozen",
            "metadata": {"artifact_ids": ["artifact-1"], "decisions": ["JSON Lines"]},
        }
    ]


def test_completion_rejects_stale_run_after_lease_recovery(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = active_mission(service)
    task = service.create_task(mission.mission_id, "Stale run", "coder", actor_id="planner")
    stale = service.claim_next("coder-a", {"coder"}, lease_seconds=5)
    assert stale is not None
    clock.value = 106.0
    service.recover_expired("watchdog")
    fresh = service.claim_next("coder-b", {"coder"}, lease_seconds=5)
    assert fresh is not None

    with pytest.raises(LeaseError, match="current run"):
        service.complete(task.task_id, stale.run.run_id, "coder-a", "late completion")


def test_paused_mission_can_resume_and_be_claimed(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = service.create_mission("Pause and resume", "operator", state="active")
    task = service.create_task(mission.mission_id, "Queued", "worker", actor_id="planner")

    paused = service.pause_mission(mission.mission_id, "operator", reason="inspect")
    assert paused.state == "paused"
    assert service.claim_next("worker-1", {"worker"}) is None

    resumed = service.resume_mission(mission.mission_id, "operator")
    claim = service.claim_next("worker-1", {"worker"})

    assert resumed.state == "active"
    assert resumed.pause_reason is None
    assert claim is not None
    assert claim.task.task_id == task.task_id


def test_cancel_mission_invalidates_running_and_queued_work_and_releases_resources(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = service.create_mission("Cancel all work", "operator", state="active")
    running = service.create_task(
        mission.mission_id,
        "Running",
        "worker",
        actor_id="planner",
        resources=("repo:main",),
    )
    queued = service.create_task(mission.mission_id, "Queued", "worker", actor_id="planner")
    claim = service.claim_next("worker-1", {"worker"})
    assert claim is not None
    assert claim.task.task_id == running.task_id

    cancelled = service.cancel_mission(mission.mission_id, "operator", reason="operator stop")

    assert cancelled.state == "cancelled"
    assert service.get_task(running.task_id).status == "cancelled"
    assert service.get_task(queued.task_id).status == "cancelled"
    assert service.list_runs(running.task_id)[0].status == "cancelled"
    with service.store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 0


def test_heartbeat_cannot_extend_run_beyond_absolute_task_runtime(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = active_mission(service)
    task = service.create_task(
        mission.mission_id,
        "Bounded runtime",
        "worker",
        actor_id="planner",
        max_runtime_seconds=5,
    )
    claim = service.claim_next("worker-1", {"worker"}, lease_seconds=100)
    assert claim is not None
    assert claim.task.lease_expires_at == 105.0
    clock.value = 101.0

    renewed = service.heartbeat(
        task.task_id,
        claim.run.run_id,
        "worker-1",
        lease_seconds=100,
    )

    assert renewed.lease_expires_at == 105.0
    clock.value = 105.1
    with pytest.raises(LeaseError, match="runtime"):
        service.heartbeat(task.task_id, claim.run.run_id, "worker-1", lease_seconds=1)
    assert service.recover_expired("watchdog") == [task.task_id]


def test_expired_lease_cannot_heartbeat_or_complete_before_recovery(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = active_mission(service)
    task = service.create_task(mission.mission_id, "Expire", "worker", actor_id="planner")
    claim = service.claim_next("worker-1", {"worker"}, lease_seconds=5)
    assert claim is not None
    clock.value = 106.0

    with pytest.raises(LeaseError, match="expired"):
        service.heartbeat(task.task_id, claim.run.run_id, "worker-1", lease_seconds=5)
    with pytest.raises(LeaseError, match="expired"):
        service.complete(task.task_id, claim.run.run_id, "worker-1", "late")


def test_workflow_idempotency_keys_reject_changed_creation_payloads(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = service.create_mission(
        "Original goal",
        "operator",
        state="active",
        idempotency_key="mission:v1",
    )
    assert service.create_mission(
        "Original goal",
        "operator",
        state="active",
        idempotency_key="mission:v1",
    ) == mission
    with pytest.raises(WorkflowError, match="idempotency"):
        service.create_mission(
            "Changed goal",
            "operator",
            state="active",
            idempotency_key="mission:v1",
        )

    task = service.create_task(
        mission.mission_id,
        "Original task",
        "worker",
        actor_id="planner",
        specification={"mode": "one"},
        idempotency_key="task:v1",
    )
    assert service.create_task(
        mission.mission_id,
        "Original task",
        "worker",
        actor_id="planner",
        specification={"mode": "one"},
        idempotency_key="task:v1",
    ) == task
    service.pause_mission(mission.mission_id, "operator", reason="inspect retry")
    assert service.create_task(
        mission.mission_id,
        "Original task",
        "worker",
        actor_id="planner",
        specification={"mode": "one"},
        idempotency_key="task:v1",
    ) == task
    with pytest.raises(WorkflowError, match="idempotency"):
        service.create_task(
            mission.mission_id,
            "Changed task",
            "worker",
            actor_id="planner",
            specification={"mode": "two"},
            idempotency_key="task:v1",
        )


def test_expired_worker_cannot_fail_current_run_before_recovery(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = active_mission(service)
    task = service.create_task(mission.mission_id, "Fence failure", "worker", actor_id="planner")
    claim = service.claim_next("worker-1", {"worker"}, lease_seconds=5)
    assert claim is not None
    clock.value = 106.0

    with pytest.raises(LeaseError, match="lease expired"):
        service.fail(task.task_id, claim.run.run_id, "worker-1", "late failure")

    assert service.get_task(task.task_id).status == "running"


def test_expired_resource_lease_is_reclaimed_atomically(tmp_path: Path) -> None:
    clock = FakeClock()
    service = make_service(tmp_path, clock)
    mission = active_mission(service)
    first = service.create_task(
        mission.mission_id,
        "First GPU user",
        "worker-a",
        actor_id="planner",
        resources=["gpu:0"],
    )
    claim = service.claim_next("worker-a-1", {"worker-a"}, lease_seconds=5)
    assert claim is not None and claim.task.task_id == first.task_id
    second = service.create_task(
        mission.mission_id,
        "Second GPU user",
        "worker-b",
        actor_id="planner",
        resources=["gpu:0"],
    )
    clock.value = 106.0

    reclaimed = service.claim_next("worker-b-1", {"worker-b"}, lease_seconds=5)

    assert reclaimed is not None
    assert reclaimed.task.task_id == second.task_id


def test_resume_promotes_children_completed_while_mission_was_paused(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)
    parent = service.create_task(mission.mission_id, "Parent", "worker", actor_id="planner")
    child = service.create_task(
        mission.mission_id,
        "Child",
        "worker",
        actor_id="planner",
        parents=[parent.task_id],
    )
    claim = service.claim_next("worker-1", {"worker"})
    assert claim is not None and claim.task.task_id == parent.task_id
    service.pause_mission(mission.mission_id, "operator", reason="inspect")
    service.complete(parent.task_id, claim.run.run_id, "worker-1", "done")
    assert service.get_task(child.task_id).status == "blocked"

    service.resume_mission(mission.mission_id, "operator")

    assert service.get_task(child.task_id).status == "ready"


@pytest.mark.parametrize("invalid_runtime", [float("inf"), float("nan")])
def test_task_runtime_must_be_finite(tmp_path: Path, invalid_runtime: float) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)

    with pytest.raises(ValueError, match="finite"):
        service.create_task(
            mission.mission_id,
            "Unbounded",
            "worker",
            actor_id="planner",
            max_runtime_seconds=invalid_runtime,
        )


def test_claim_and_heartbeat_leases_must_be_finite(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mission = active_mission(service)
    task = service.create_task(mission.mission_id, "Finite lease", "worker", actor_id="planner")

    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            service.claim_next("worker-1", {"worker"}, lease_seconds=value)
    claim = service.claim_next("worker-1", {"worker"}, lease_seconds=5)
    assert claim is not None and claim.task.task_id == task.task_id
    with pytest.raises(ValueError, match="finite"):
        service.heartbeat(
            task.task_id,
            claim.run.run_id,
            "worker-1",
            lease_seconds=float("nan"),
        )
