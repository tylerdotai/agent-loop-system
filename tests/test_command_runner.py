import sys
from pathlib import Path

from agent_loop.command_runner import run_command_loop


def test_command_loop_passes_state_between_worker_and_evaluator(tmp_path: Path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        """
import json
import sys
state = json.load(sys.stdin)
print(json.dumps({"output": f"draft-{state['attempt']}", "metadata": {"seen_goal": state["goal"]}}))
""".strip()
    )
    evaluator = tmp_path / "evaluator.py"
    evaluator.write_text(
        """
import json
import sys
payload = json.load(sys.stdin)
passed = payload["result"]["output"] == "draft-2"
print(json.dumps({"passed": passed, "message": "accepted" if passed else "revise"}))
""".strip()
    )

    report = run_command_loop(
        goal="ship the thing",
        max_iterations=3,
        work_command=[sys.executable, str(worker)],
        eval_command=[sys.executable, str(evaluator)],
    )

    assert report.success is True
    assert report.iterations == 2
    assert report.history[0].eval_message == "revise"
    assert report.history[1].metadata == {"seen_goal": "ship the thing"}
