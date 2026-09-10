from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from agent_loop.action_broker import (
    ActionBroker,
    ActionHandler,
    ActionOutcomeUnknown,
    ApprovalError,
    BudgetExceededError,
    BudgetService,
    CapabilityError,
    IdempotencyConflictError,
)
from agent_loop.persistence import SQLiteStore
from agent_loop.workflow import WorkflowService


POLICY = {
    "artifact.write": "R1",
    "host.service.restart": "R2",
    "external.send": "R3",
    "policy.modify": "R4",
}


class RecordingHandler(ActionHandler):
    def __init__(self, *, fail_verify: bool = False) -> None:
        self.executions = 0
        self.fail_verify = fail_verify

    def execute(self, request) -> dict[str, Any]:
        self.executions += 1
        return {"target": request.target, "changed": True}

    def verify(self, request, result: dict[str, Any]) -> dict[str, Any]:
        if self.fail_verify:
            raise ActionOutcomeUnknown("target readback unavailable")
        return {"verified": result["changed"], "target": request.target}


class SecretFailingHandler(ActionHandler):
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def execute(self, request) -> dict[str, Any]:
        raise RuntimeError(f"provider rejected {self.secret}")

    def verify(self, request, result: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("verify must not run after execution failure")


def make_broker(tmp_path: Path) -> ActionBroker:
    return ActionBroker(SQLiteStore(tmp_path / "control.db"), risk_policy=POLICY)


def _reserve_budget(database: str, reservation_id: str, output: Any) -> None:
    service = BudgetService(SQLiteStore(database))
    try:
        service.reserve("mission", "mission-1", "tokens", 60, reservation_id)
    except BudgetExceededError:
        output.put((reservation_id, "exceeded"))
    else:
        output.put((reservation_id, "reserved"))


def test_exact_capability_is_required_and_r1_is_authorized_without_approval(tmp_path: Path) -> None:
    broker = make_broker(tmp_path)
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")

    request = broker.propose(
        actor_id="builder-1",
        mission_id="mission-1",
        action_type="artifact.write",
        target={"path": "report.md"},
        arguments={"artifact_id": "artifact-1"},
        idempotency_key="task-1:artifact-write:v1",
    )

    assert request.risk_class == "R1"
    assert request.status == "authorized"

    with pytest.raises(CapabilityError, match="not granted"):
        broker.propose(
            actor_id="builder-2",
            mission_id="mission-1",
            action_type="artifact.write",
            target={"path": "other.md"},
            arguments={},
            idempotency_key="task-2:artifact-write:v1",
        )


def test_r2_approval_binds_the_exact_canonical_payload_hash(tmp_path: Path) -> None:
    broker = make_broker(tmp_path)
    broker.grant_capabilities("ops-1", {"host.service.restart"}, actor_id="operator")
    request = broker.propose(
        actor_id="ops-1",
        mission_id="mission-1",
        action_type="host.service.restart",
        target={"service": "example.service"},
        arguments={"mode": "graceful"},
        idempotency_key="restart:example:v1",
    )

    assert request.status == "awaiting_approval"

    with pytest.raises(ApprovalError, match="payload hash"):
        broker.approve(request.action_id, "operator", payload_hash="wrong")

    approved = broker.approve(request.action_id, "operator", payload_hash=request.payload_hash)
    assert approved.status == "authorized"
    assert approved.approved_by == "operator"


def test_idempotency_key_cannot_hide_changed_target_or_arguments(tmp_path: Path) -> None:
    broker = make_broker(tmp_path)
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")
    original = broker.propose(
        "builder-1",
        "mission-1",
        "artifact.write",
        {"path": "a.md"},
        {"content_hash": "one"},
        idempotency_key="write:v1",
    )
    duplicate = broker.propose(
        "builder-1",
        "mission-1",
        "artifact.write",
        {"path": "a.md"},
        {"content_hash": "one"},
        idempotency_key="write:v1",
    )

    assert duplicate == original

    with pytest.raises(IdempotencyConflictError, match="different payload"):
        broker.propose(
            "builder-1",
            "mission-1",
            "artifact.write",
            {"path": "b.md"},
            {"content_hash": "two"},
            idempotency_key="write:v1",
        )


def test_r4_action_is_denied_even_when_capability_was_registered(tmp_path: Path) -> None:
    broker = make_broker(tmp_path)
    broker.grant_capabilities("agent-1", {"policy.modify"}, actor_id="operator")

    request = broker.propose(
        "agent-1",
        "mission-1",
        "policy.modify",
        {"policy": "active"},
        {"change": "allow-all"},
        idempotency_key="policy-change:v1",
    )

    assert request.status == "denied"
    with pytest.raises(ApprovalError, match="not authorized"):
        broker.approve(request.action_id, "operator", payload_hash=request.payload_hash)


def test_verified_action_executes_once_and_returns_same_receipt_on_retry(tmp_path: Path) -> None:
    broker = make_broker(tmp_path)
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")
    request = broker.propose(
        "builder-1",
        "mission-1",
        "artifact.write",
        {"path": "report.md"},
        {"artifact_id": "artifact-1"},
        idempotency_key="write-report:v1",
    )
    handler = RecordingHandler()

    first = broker.execute(request.action_id, {"artifact.write": handler})
    second = broker.execute(request.action_id, {"artifact.write": handler})

    assert first.status == "verified"
    assert first.verification["verified"] is True
    assert second == first
    assert handler.executions == 1


def test_effect_with_unavailable_readback_becomes_unknown_and_is_not_retried(tmp_path: Path) -> None:
    broker = make_broker(tmp_path)
    broker.grant_capabilities("sender-1", {"external.send"}, actor_id="operator")
    request = broker.propose(
        "sender-1",
        "mission-1",
        "external.send",
        {"channel": "test", "recipient": "example"},
        {"body_hash": "abc"},
        idempotency_key="send:test:abc",
    )
    broker.approve(request.action_id, "operator", payload_hash=request.payload_hash)
    handler = RecordingHandler(fail_verify=True)

    receipt = broker.execute(request.action_id, {"external.send": handler})

    assert receipt.status == "unknown"
    assert "readback unavailable" in (receipt.error or "")
    assert handler.executions == 1
    assert broker.execute(request.action_id, {"external.send": handler}) == receipt
    assert handler.executions == 1


def test_action_handler_error_redacts_configured_secret_values(tmp_path: Path) -> None:
    secret = "provider-secret-value"
    broker = ActionBroker(
        SQLiteStore(tmp_path / "control.db"),
        risk_policy=POLICY,
        redact_values={secret},
    )
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")
    request = broker.propose(
        "builder-1",
        "mission-1",
        "artifact.write",
        {"path": "report.md"},
        {},
        idempotency_key="redacted-failure:v1",
    )

    receipt = broker.execute(
        request.action_id,
        {"artifact.write": SecretFailingHandler(secret)},
    )

    assert receipt.status == "unknown"
    assert secret not in (receipt.error or "")
    assert "[REDACTED]" in (receipt.error or "")


def test_post_effect_result_validation_failure_is_unknown(tmp_path: Path) -> None:
    marker = tmp_path / "effect-happened"

    class InvalidResultHandler(ActionHandler):
        def execute(self, request) -> dict[str, Any]:
            marker.write_text("changed")
            return {"token": "must-not-persist"}

        def verify(self, request, result: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("invalid execution result must not be verified")

    broker = make_broker(tmp_path)
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")
    request = broker.propose(
        "builder-1",
        "mission-1",
        "artifact.write",
        {"path": "report.md"},
        {},
        idempotency_key="post-effect-invalid-result:v1",
    )

    receipt = broker.execute(request.action_id, {"artifact.write": InvalidResultHandler()})

    assert marker.read_text() == "changed"
    assert receipt.status == "unknown"
    assert receipt.result == {}
    assert "secret field" in (receipt.error or "")

def test_action_linked_to_task_requires_current_owned_run(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store)
    mission = workflow.create_mission("Action task", "operator", state="active")
    task = workflow.create_task(mission.mission_id, "Write", "builder", actor_id="planner")
    claim = workflow.claim_next("builder-1", {"builder"})
    assert claim is not None
    broker = ActionBroker(store, risk_policy=POLICY)
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")

    request = broker.propose(
        "builder-1",
        mission.mission_id,
        "artifact.write",
        {"path": "report.md"},
        {},
        task_id=task.task_id,
        run_id=claim.run.run_id,
        idempotency_key="task-action:v1",
    )
    assert request.status == "authorized"

    workflow.complete(task.task_id, claim.run.run_id, "builder-1", "done")
    with pytest.raises(CapabilityError, match="current active run"):
        broker.propose(
            "builder-1",
            mission.mission_id,
            "artifact.write",
            {"path": "late.md"},
            {},
            task_id=task.task_id,
            run_id=claim.run.run_id,
            idempotency_key="late-action:v1",
        )

    handler = RecordingHandler()
    with pytest.raises(CapabilityError, match="current active run"):
        broker.execute(request.action_id, {"artifact.write": handler})
    assert handler.executions == 0


def test_action_cannot_execute_after_linked_run_lease_expires(tmp_path: Path) -> None:
    now = [100.0]

    def clock() -> float:
        return now[0]

    store = SQLiteStore(tmp_path / "control.db")
    workflow = WorkflowService(store, clock=clock)
    mission = workflow.create_mission("Lease-bound action", "operator", state="active")
    task = workflow.create_task(mission.mission_id, "Write", "builder", actor_id="planner")
    claim = workflow.claim_next("builder-1", {"builder"}, lease_seconds=5)
    assert claim is not None
    broker = ActionBroker(store, risk_policy=POLICY, clock=clock)
    broker.grant_capabilities("builder-1", {"artifact.write"}, actor_id="operator")
    request = broker.propose(
        "builder-1",
        mission.mission_id,
        "artifact.write",
        {"path": "report.md"},
        {},
        task_id=task.task_id,
        run_id=claim.run.run_id,
        idempotency_key="lease-action:v1",
    )
    now[0] = 106.0
    handler = RecordingHandler()

    with pytest.raises(CapabilityError, match="current active run"):
        broker.execute(request.action_id, {"artifact.write": handler})

    assert handler.executions == 0


def test_budget_reservations_are_atomic_and_idempotent_across_processes(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    budgets = BudgetService(SQLiteStore(database))
    budgets.set_limit("mission", "mission-1", "tokens", 100, actor_id="operator")
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(target=_reserve_budget, args=(str(database), f"reservation-{index}", output))
        for index in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    results = [output.get(timeout=2) for _ in processes]
    assert sorted(status for _, status in results) == ["exceeded", "reserved"]
    budget = budgets.get("mission", "mission-1", "tokens")
    assert budget.limit_value == 100
    assert budget.reserved_value == 60
    assert budget.used_value == 0

    winner = next(reservation_id for reservation_id, status in results if status == "reserved")
    same = budgets.reserve("mission", "mission-1", "tokens", 60, winner)
    assert same.amount == 60


def test_budget_reservations_are_consumed_or_released_without_losing_accounting(tmp_path: Path) -> None:
    budgets = BudgetService(SQLiteStore(tmp_path / "control.db"))
    budgets.set_limit("mission", "mission-1", "actions", 10, actor_id="operator")
    consumed = budgets.reserve("mission", "mission-1", "actions", 4, "consume-me")
    released = budgets.reserve("mission", "mission-1", "actions", 3, "release-me")

    budgets.consume(consumed.reservation_id)
    budgets.release(released.reservation_id)

    budget = budgets.get("mission", "mission-1", "actions")
    assert budget.used_value == 4
    assert budget.reserved_value == 0
    assert budgets.get_reservation(consumed.reservation_id).status == "consumed"
    assert budgets.get_reservation(released.reservation_id).status == "released"


def test_budget_amounts_must_be_finite(tmp_path: Path) -> None:
    budgets = BudgetService(SQLiteStore(tmp_path / "control.db"))

    with pytest.raises(ValueError, match="finite"):
        budgets.set_limit("mission", "mission-1", "runs", float("inf"), actor_id="operator")
    budgets.set_limit("mission", "mission-1", "runs", 2, actor_id="operator")
    with pytest.raises(ValueError, match="finite"):
        budgets.reserve(
            "mission",
            "mission-1",
            "runs",
            float("nan"),
            reservation_id="invalid-budget:v1",
        )
