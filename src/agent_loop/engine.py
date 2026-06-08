from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class LoopSpec:
    goal: str
    max_iterations: int = 5
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoryEntry:
    attempt: int
    output: str
    passed: bool
    eval_message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoopReport:
    goal: str
    success: bool
    iterations: int
    stop_reason: str
    history: list[HistoryEntry]


Worker = Callable[[dict[str, Any]], ActionResult]
Evaluator = Callable[[ActionResult, dict[str, Any]], tuple[bool, str]]


class LoopEngine:
    def __init__(self, worker: Worker, evaluator: Evaluator) -> None:
        self.worker = worker
        self.evaluator = evaluator

    def run(self, spec: LoopSpec) -> LoopReport:
        goal = spec.goal.strip()
        if not goal:
            raise ValueError("goal must not be empty")
        if spec.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")

        history: list[HistoryEntry] = []
        feedback: str | None = None

        for attempt in range(1, spec.max_iterations + 1):
            state: dict[str, Any] = {
                "goal": goal,
                "attempt": attempt,
                "max_iterations": spec.max_iterations,
                "context": dict(spec.context),
                "previous_feedback": feedback,
                "history": list(history),
            }
            result = self.worker(state)
            passed, message = self.evaluator(result, state)
            entry = HistoryEntry(
                attempt=attempt,
                output=result.output,
                passed=passed,
                eval_message=message,
                metadata=result.metadata,
            )
            history.append(entry)
            if passed:
                return LoopReport(
                    goal=goal,
                    success=True,
                    iterations=attempt,
                    stop_reason="eval_passed",
                    history=history,
                )
            feedback = message

        return LoopReport(
            goal=goal,
            success=False,
            iterations=spec.max_iterations,
            stop_reason="max_iterations",
            history=history,
        )
