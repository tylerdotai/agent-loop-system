from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .action_broker import BudgetExceededError, BudgetService
from .artifacts import ArtifactStore
from .runner_adapter import JsonSubprocessRunner, RunRequest, RunnerResult
from .workflow import LeaseError, Task, WorkflowService
from .worker_api import RunTokenService


@dataclass(frozen=True)
class CoordinatorResult:
    task_id: str
    run_id: str
    status: str
    summary: str | None = None
    error: str | None = None


class Coordinator:
    """Claim one durable task at a time and execute it through a runner adapter."""

    def __init__(
        self,
        workflow: WorkflowService,
        runner: JsonSubprocessRunner,
        *,
        worker_id: str,
        roles: Iterable[str],
        lease_seconds: float = 900,
        artifacts: ArtifactStore | None = None,
        budgets: BudgetService | None = None,
        run_tokens: RunTokenService | None = None,
        control_socket_path: str | Path | None = None,
        capabilities_by_role: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.workflow = workflow
        self.runner = runner
        self.worker_id = worker_id
        self.roles = frozenset(roles)
        self.lease_seconds = lease_seconds
        self.artifacts = artifacts
        self.budgets = budgets
        self.run_tokens = run_tokens
        self.control_socket_path = (
            None if control_socket_path is None else Path(control_socket_path).expanduser().resolve()
        )
        self.capabilities_by_role = {
            role: frozenset(capabilities)
            for role, capabilities in (capabilities_by_role or {}).items()
        }
        if not self.worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if not self.roles or not all(isinstance(role, str) and role for role in self.roles):
            raise ValueError("roles must contain at least one non-empty role")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be positive and finite")
        scoped_api_parts = (self.run_tokens, self.control_socket_path, capabilities_by_role)
        if any(part is not None for part in scoped_api_parts) and not all(
            part is not None for part in scoped_api_parts
        ):
            raise ValueError(
                "run_tokens, control_socket_path, and capabilities_by_role must be configured together"
            )

    def run_once(self) -> CoordinatorResult | None:
        self.workflow.recover_expired("coordinator")
        claim = self.workflow.claim_next(
            self.worker_id,
            self.roles,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return None
        task = claim.task
        run = claim.run
        token_issued = False
        heartbeat_stop: threading.Event | None = None
        heartbeat_thread: threading.Thread | None = None
        try:
            if self.budgets is not None:
                try:
                    self.budgets.charge_if_configured(
                        "mission",
                        task.mission_id,
                        "runs",
                        1,
                        f"run:{run.run_id}",
                    )
                except BudgetExceededError as exc:
                    error = str(exc)
                    self.workflow.fail(
                        task.task_id,
                        run.run_id,
                        self.worker_id,
                        error,
                        outcome="budget_exceeded",
                    )
                    return CoordinatorResult(task.task_id, run.run_id, "failed", error=error)
            command, timeout_seconds, cwd, env = self._execution_spec(task)
            mission = self.workflow.get_mission(task.mission_id)
            control: dict[str, Any] = {}
            if self.run_tokens is not None and self.control_socket_path is not None:
                capabilities = self.capabilities_by_role.get(task.assignee)
                if not capabilities:
                    raise ValueError(f"no worker API capabilities configured for role: {task.assignee}")
                issued = self.run_tokens.issue(
                    run.run_id,
                    capabilities,
                    expires_in_seconds=max(self.lease_seconds, task.max_runtime_seconds),
                )
                token_issued = True
                control = {"socket_path": str(self.control_socket_path), "token": issued.token}
            request = RunRequest(
                mission_id=task.mission_id,
                task_id=task.task_id,
                run_id=run.run_id,
                worker_id=self.worker_id,
                goal=mission.goal,
                specification=task.specification,
                acceptance=task.acceptance,
                context=self.workflow.build_task_context(task.task_id),
                workspace=cwd,
                limits={
                    "max_runtime_seconds": task.max_runtime_seconds,
                    "max_attempts": task.max_attempts,
                    **mission.limits,
                },
                control=control,
            )
            heartbeat_interval = max(0.001, min(self.lease_seconds / 4, 30.0))
            heartbeat_stop = threading.Event()
            heartbeat_errors: list[BaseException] = []

            def renew_run() -> None:
                self.workflow.heartbeat(
                    task.task_id,
                    run.run_id,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                )

            def heartbeat_loop() -> None:
                assert heartbeat_stop is not None
                while not heartbeat_stop.wait(heartbeat_interval):
                    try:
                        renew_run()
                    except BaseException as exc:
                        heartbeat_errors.append(exc)
                        heartbeat_stop.set()
                        return

            def maintain_run() -> bool:
                if heartbeat_errors:
                    raise heartbeat_errors[0]
                current = self.workflow.get_task(task.task_id)
                if current.status != "running" or current.current_run_id != run.run_id:
                    return True
                return False

            renew_run()
            heartbeat_thread = threading.Thread(
                target=heartbeat_loop,
                name=f"agent-loop-heartbeat-{run.run_id}",
                daemon=True,
            )
            heartbeat_thread.start()

            result = self.runner.run(
                request,
                command,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
                env=env,
                cancel_requested=maintain_run,
            )
            renew_run()
            validation_error = self._validate_candidate(task, result)
            if validation_error is not None:
                self.workflow.fail(
                    task.task_id,
                    run.run_id,
                    self.worker_id,
                    validation_error,
                    outcome="validation_failed",
                )
                return CoordinatorResult(task.task_id, run.run_id, "failed", error=validation_error)
            completion_metadata = result.metadata()
            verification_command = task.acceptance.get("verification_command")
            if verification_command is not None:
                if not isinstance(verification_command, list):
                    error = "acceptance.verification_command must be a command array"
                    self.workflow.fail(
                        task.task_id,
                        run.run_id,
                        self.worker_id,
                        error,
                        outcome="verification_failed",
                    )
                    return CoordinatorResult(task.task_id, run.run_id, "failed", error=error)
                verification_timeout = task.acceptance.get(
                    "verification_timeout_seconds",
                    min(300.0, task.max_runtime_seconds),
                )
                if (
                    isinstance(verification_timeout, bool)
                    or not isinstance(verification_timeout, (int, float))
                    or not math.isfinite(float(verification_timeout))
                    or verification_timeout <= 0
                ):
                    error = "acceptance.verification_timeout_seconds must be positive"
                    self.workflow.fail(
                        task.task_id,
                        run.run_id,
                        self.worker_id,
                        error,
                        outcome="verification_failed",
                    )
                    return CoordinatorResult(task.task_id, run.run_id, "failed", error=error)
                try:
                    verification = self.runner.run_check(
                        request,
                        verification_command,
                        timeout_seconds=min(
                            float(verification_timeout),
                            task.max_runtime_seconds,
                        ),
                        cwd=cwd,
                        env=env,
                        cancel_requested=maintain_run,
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    self.workflow.fail(
                        task.task_id,
                        run.run_id,
                        self.worker_id,
                        error,
                        outcome="verification_failed",
                    )
                    return CoordinatorResult(task.task_id, run.run_id, "failed", error=error)
                if verification.exit_code != 0:
                    detail = verification.stderr or verification.stdout
                    suffix = f": {detail}" if detail else ""
                    error = f"verification command failed ({verification.exit_code}){suffix}"
                    self.workflow.fail(
                        task.task_id,
                        run.run_id,
                        self.worker_id,
                        error,
                        outcome="verification_failed",
                    )
                    return CoordinatorResult(task.task_id, run.run_id, "failed", error=error)
                renew_run()
                completion_metadata["verification"] = verification.metadata()
            self.workflow.complete(
                task.task_id,
                run.run_id,
                self.worker_id,
                result.summary,
                metadata=completion_metadata,
            )
            return CoordinatorResult(task.task_id, run.run_id, "completed", summary=result.summary)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            current = self.workflow.get_task(task.task_id)
            if current.status == "running" and current.current_run_id == run.run_id:
                try:
                    self.workflow.fail(
                        task.task_id,
                        run.run_id,
                        self.worker_id,
                        error,
                        outcome="runner_failed",
                    )
                except LeaseError:
                    pass
            return CoordinatorResult(task.task_id, run.run_id, "failed", error=error)
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2)
            if token_issued and self.run_tokens is not None:
                self.run_tokens.revoke_run(run.run_id, actor_id="coordinator")

    def run_until_idle(self, *, max_tasks: int) -> list[CoordinatorResult]:
        if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 1:
            raise ValueError("max_tasks must be a positive integer")
        results: list[CoordinatorResult] = []
        for _ in range(max_tasks):
            result = self.run_once()
            if result is None:
                break
            results.append(result)
        return results

    def run_daemon(
        self,
        *,
        poll_seconds: float = 1,
        stop_requested: Callable[[], bool] = lambda: False,
        max_cycles: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(float(poll_seconds))
            or poll_seconds < 0
        ):
            raise ValueError("poll_seconds must be finite and not negative")
        if max_cycles is not None and (
            isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1
        ):
            raise ValueError("max_cycles must be a positive integer when provided")
        processed = 0
        cycle = 0
        while not stop_requested():
            result = self.run_once()
            cycle += 1
            if result is not None:
                processed += 1
            if stop_requested() or (max_cycles is not None and cycle >= max_cycles):
                break
            if result is None:
                sleep(float(poll_seconds))
        return processed

    def _execution_spec(
        self,
        task: Task,
    ) -> tuple[Sequence[str], float, str, dict[str, str] | None]:
        command = task.specification.get("command")
        if not isinstance(command, list):
            raise ValueError("task specification.command must be a command array")
        configured_timeout = task.specification.get("timeout_seconds", task.max_runtime_seconds)
        if (
            isinstance(configured_timeout, bool)
            or not isinstance(configured_timeout, (int, float))
            or not math.isfinite(float(configured_timeout))
            or configured_timeout <= 0
        ):
            raise ValueError("task specification.timeout_seconds must be positive")
        timeout_seconds = min(float(configured_timeout), task.max_runtime_seconds)
        cwd_value = task.specification.get("cwd")
        if cwd_value is None:
            if self.runner.allowed_workspace_roots:
                workspace_root = self.runner.allowed_workspace_roots[0]
            else:
                workspace_root = self.workflow.store.path.parent / "workspaces"
            workspace = workspace_root / task.mission_id / task.task_id
            cwd = str(self.runner.prepare_workspace(workspace))
        elif isinstance(cwd_value, str) and cwd_value:
            cwd = str(Path(cwd_value).expanduser().resolve())
        else:
            raise ValueError("task specification.cwd must be a non-empty string")
        env = task.specification.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError("task specification.env must be an object")
        return command, timeout_seconds, cwd, env

    def _validate_candidate(self, task: Task, result: RunnerResult) -> str | None:
        if result.outcome != "candidate_complete":
            detail = f": {result.summary}" if result.summary else ""
            return f"runner outcome is not a completion candidate: {result.outcome}{detail}"
        required = task.acceptance.get("required_evidence_kinds", [])
        if not isinstance(required, list) or not all(isinstance(kind, str) and kind for kind in required):
            return "acceptance.required_evidence_kinds must be a list of non-empty strings"
        present = {str(item.get("kind")) for item in result.evidence}
        missing = sorted(set(required) - present)
        if missing:
            return f"required evidence is missing: {', '.join(missing)}"
        minimum_artifacts = task.acceptance.get("minimum_artifacts", 0)
        if isinstance(minimum_artifacts, bool) or not isinstance(minimum_artifacts, int) or minimum_artifacts < 0:
            return "acceptance.minimum_artifacts must be a non-negative integer"
        if len(result.artifact_ids) < minimum_artifacts:
            return f"required artifact count is {minimum_artifacts}, received {len(result.artifact_ids)}"
        artifacts = self.artifacts
        if not result.artifact_ids:
            return None
        if artifacts is None:
            return "runner returned artifact IDs but no artifact store is configured"
        for artifact_id in result.artifact_ids:
            try:
                artifact = artifacts.get(artifact_id)
                artifacts.read_bytes(artifact_id)
            except (ValueError, RuntimeError) as exc:
                return f"artifact validation failed for {artifact_id}: {exc}"
            if artifact.mission_id != task.mission_id or artifact.task_id != task.task_id:
                return f"artifact is outside the task scope: {artifact_id}"
        return None
