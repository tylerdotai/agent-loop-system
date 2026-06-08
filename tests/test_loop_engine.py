from agent_loop import ActionResult, LoopEngine, LoopSpec


def test_loop_runs_until_eval_passes_and_records_each_attempt():
    attempts = []

    def worker(state):
        attempt = state["attempt"]
        attempts.append(attempt)
        return ActionResult(output=f"draft-{attempt}", metadata={"attempt": attempt})

    def evaluator(result, state):
        return result.output == "draft-3", "needs third draft"

    spec = LoopSpec(goal="produce accepted draft", max_iterations=5)
    report = LoopEngine(worker=worker, evaluator=evaluator).run(spec)

    assert report.success is True
    assert report.iterations == 3
    assert [entry.output for entry in report.history] == ["draft-1", "draft-2", "draft-3"]
    assert attempts == [1, 2, 3]


def test_loop_stops_at_max_iterations_with_failed_report():
    def worker(state):
        return ActionResult(output=f"attempt-{state['attempt']}")

    def evaluator(result, state):
        return False, "not good enough"

    spec = LoopSpec(goal="impossible goal", max_iterations=2)
    report = LoopEngine(worker=worker, evaluator=evaluator).run(spec)

    assert report.success is False
    assert report.iterations == 2
    assert report.stop_reason == "max_iterations"
    assert [entry.eval_message for entry in report.history] == ["not good enough", "not good enough"]


def test_loop_rejects_empty_goals():
    spec = LoopSpec(goal="   ", max_iterations=1)

    def worker(state):
        return ActionResult(output="unused")

    def evaluator(result, state):
        return True, "unused"

    try:
        LoopEngine(worker=worker, evaluator=evaluator).run(spec)
    except ValueError as exc:
        assert "goal" in str(exc)
    else:
        raise AssertionError("expected ValueError")
