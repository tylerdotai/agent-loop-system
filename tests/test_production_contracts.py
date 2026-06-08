import json
import sys
from pathlib import Path

from agent_loop.cli import main
from agent_loop.command_runner import run_command_loop


def write_script(path: Path, source: str) -> Path:
    path.write_text(source.strip() + "\n")
    return path


def test_evaluator_passed_must_be_json_boolean(tmp_path: Path):
    worker = write_script(
        tmp_path / "worker.py",
        """
import json
print(json.dumps({"output": "done"}))
""",
    )
    evaluator = write_script(
        tmp_path / "evaluator.py",
        """
import json
print(json.dumps({"passed": "false", "message": "string false must not pass"}))
""",
    )

    try:
        run_command_loop(
            goal="strict boolean gate",
            max_iterations=1,
            work_command=[sys.executable, str(worker)],
            eval_command=[sys.executable, str(evaluator)],
        )
    except ValueError as exc:
        assert "passed" in str(exc)
        assert "boolean" in str(exc)
    else:
        raise AssertionError("expected evaluator passed type validation")


def test_commands_must_be_non_empty_lists_of_strings(tmp_path: Path):
    spec = tmp_path / "loop.json"
    spec.write_text(
        json.dumps(
            {
                "goal": "reject string command",
                "max_iterations": 1,
                "work_command": "python worker.py",
                "eval_command": [sys.executable, "evaluator.py"],
            }
        )
    )

    exit_code = main([str(spec)])

    assert exit_code == 2


def test_timeout_must_be_positive_integer(tmp_path: Path):
    worker = write_script(
        tmp_path / "worker.py",
        """
import json
print(json.dumps({"output": "done"}))
""",
    )
    evaluator = write_script(
        tmp_path / "evaluator.py",
        """
import json
print(json.dumps({"passed": true, "message": "ok"}))
""",
    )

    try:
        run_command_loop(
            goal="reject bad timeout",
            max_iterations=1,
            work_command=[sys.executable, str(worker)],
            eval_command=[sys.executable, str(evaluator)],
            timeout_seconds=0,
        )
    except ValueError as exc:
        assert "timeout_seconds" in str(exc)
    else:
        raise AssertionError("expected timeout validation")


def test_cli_reports_invalid_json_spec_without_traceback(tmp_path: Path, capsys):
    spec = tmp_path / "bad.json"
    spec.write_text("{")

    exit_code = main([str(spec)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid spec" in captured.err
    assert "Traceback" not in captured.err
