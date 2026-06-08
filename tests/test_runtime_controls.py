import sys
from pathlib import Path

from agent_loop.command_runner import run_command_loop


def write_script(path: Path, source: str) -> Path:
    path.write_text(source.strip() + "\n")
    return path


def test_command_runner_applies_cwd_and_environment_to_real_subprocesses(tmp_path: Path):
    worker = write_script(
        tmp_path / "worker.py",
        """
import json
import os
print(json.dumps({"output": json.dumps({"cwd": os.getcwd(), "marker": os.environ.get("AGENT_LOOP_MARKER")})}))
""",
    )
    evaluator = write_script(
        tmp_path / "evaluator.py",
        """
import json
import sys
payload = json.load(sys.stdin)
observed = json.loads(payload["result"]["output"])
expected = payload["state"]["context"]
passed = observed["cwd"] == expected["cwd"] and observed["marker"] == "production"
print(json.dumps({"passed": passed, "message": "runtime controls honored"}))
""",
    )

    report = run_command_loop(
        goal="honor runtime controls",
        max_iterations=1,
        work_command=[sys.executable, str(worker)],
        eval_command=[sys.executable, str(evaluator)],
        command_cwd=str(tmp_path),
        command_env={"AGENT_LOOP_MARKER": "production"},
        context={"cwd": str(tmp_path)},
    )

    assert report.success is True


def test_command_failure_message_is_capped_to_tail_of_output(tmp_path: Path):
    worker = write_script(
        tmp_path / "noisy_worker.py",
        """
import sys
sys.stderr.write("A" * 500 + "TAIL")
sys.exit(9)
""",
    )
    evaluator = write_script(
        tmp_path / "evaluator.py",
        """
import json
print(json.dumps({"passed": true, "message": "should not run"}))
""",
    )

    try:
        run_command_loop(
            goal="cap noisy failures",
            max_iterations=1,
            work_command=[sys.executable, str(worker)],
            eval_command=[sys.executable, str(evaluator)],
            max_output_chars=32,
        )
    except RuntimeError as exc:
        message = str(exc)
        assert len(message) < 120
        assert "TAIL" in message
    else:
        raise AssertionError("expected capped command failure")


def test_command_timeout_is_reported_as_timeout_error(tmp_path: Path):
    worker = write_script(
        tmp_path / "slow_worker.py",
        """
import time
time.sleep(2)
""",
    )
    evaluator = write_script(
        tmp_path / "evaluator.py",
        """
import json
print(json.dumps({"passed": true, "message": "should not run"}))
""",
    )

    try:
        run_command_loop(
            goal="timeout real subprocess",
            max_iterations=1,
            work_command=[sys.executable, str(worker)],
            eval_command=[sys.executable, str(evaluator)],
            timeout_seconds=1,
        )
    except TimeoutError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected subprocess timeout")
