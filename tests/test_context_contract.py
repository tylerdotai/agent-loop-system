import sys
from pathlib import Path

from agent_loop.command_runner import run_command_loop


def test_context_is_passed_to_real_worker_command(tmp_path: Path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        """
import json
import sys
state = json.load(sys.stdin)
print(json.dumps({"output": state["context"]["artifact_name"]}))
""".strip()
        + "\n"
    )
    evaluator = tmp_path / "evaluator.py"
    evaluator.write_text(
        """
import json
import sys
payload = json.load(sys.stdin)
print(json.dumps({"passed": payload["result"]["output"] == "production", "message": "checked context"}))
""".strip()
        + "\n"
    )

    report = run_command_loop(
        goal="carry production context",
        max_iterations=1,
        work_command=[sys.executable, str(worker)],
        eval_command=[sys.executable, str(evaluator)],
        context={"artifact_name": "production"},
    )

    assert report.success is True
