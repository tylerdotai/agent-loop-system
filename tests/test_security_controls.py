import json
import stat
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from agent_loop.command_runner import run_command_loop


def write_script(path: Path, source: str) -> Path:
    path.write_text(dedent(source).strip() + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def passing_evaluator(path: Path) -> Path:
    return write_script(
        path,
        """
        import json
        print(json.dumps({"passed": True, "message": "ok"}))
        """,
    )


def test_allowed_commands_blocks_unapproved_worker_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    worker = write_script(
        tmp_path / "worker.py",
        f"""
        from pathlib import Path
        Path({str(marker)!r}).write_text("executed")
        print("never")
        """,
    )
    evaluator = passing_evaluator(tmp_path / "eval.py")

    with pytest.raises(ValueError, match="not allowed by policy"):
        run_command_loop(
            goal="enforce allowlist",
            max_iterations=1,
            work_command=[sys.executable, str(worker)],
            eval_command=[sys.executable, str(evaluator)],
            allowed_commands=["definitely-not-python"],
        )

    assert not marker.exists()


def test_redacts_configured_sensitive_values_from_history_and_eval_message(tmp_path: Path) -> None:
    sensitive_value = "sensitive-value-to-redact"
    worker = write_script(
        tmp_path / "worker.py",
        f"""
        import json
        print(json.dumps({{"output": "worker leaked {sensitive_value}", "metadata": {{"raw": "{sensitive_value}"}}}}))
        """,
    )
    evaluator = write_script(
        tmp_path / "eval.py",
        f"""
        import json
        print(json.dumps({{"passed": False, "message": "eval leaked {sensitive_value}"}}))
        """,
    )

    report = run_command_loop(
        goal="redact outputs",
        max_iterations=1,
        work_command=[sys.executable, str(worker)],
        eval_command=[sys.executable, str(evaluator)],
        redact_values=[sensitive_value],
    )

    entry = report.history[0]
    assert sensitive_value not in entry.output
    assert sensitive_value not in entry.eval_message
    assert sensitive_value not in json.dumps(entry.metadata)
    assert "[REDACTED]" in entry.output
    assert "[REDACTED]" in entry.eval_message
    assert entry.metadata["raw"] == "[REDACTED]"


def test_command_env_values_are_redacted_without_explicit_redact_values(tmp_path: Path) -> None:
    sensitive_env_value = "env-sensitive-value-should-not-enter-report"
    worker = write_script(
        tmp_path / "worker.py",
        """
        import json
        import os
        print(json.dumps({"output": os.environ["LOOP_SECRET"]}))
        """,
    )
    evaluator = passing_evaluator(tmp_path / "eval.py")

    report = run_command_loop(
        goal="redact env",
        max_iterations=1,
        work_command=[sys.executable, str(worker)],
        eval_command=[sys.executable, str(evaluator)],
        command_env={"LOOP_SECRET": sensitive_env_value},
    )

    assert sensitive_env_value not in report.history[0].output
    assert report.history[0].output == "[REDACTED]"


def test_container_config_wraps_commands_with_runtime_and_image(tmp_path: Path) -> None:
    log_path = tmp_path / "container-argv.jsonl"
    runtime = write_script(
        tmp_path / "container-runtime.py",
        f'''#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

with Path({str(log_path)!r}).open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
image_index = args.index("python:3.11-slim")
inner = args[image_index + 1:]
completed = subprocess.run(inner, input=sys.stdin.read(), text=True, capture_output=True, check=False)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
raise SystemExit(completed.returncode)
''',
    )
    worker = write_script(
        tmp_path / "worker.py",
        """
        import json
        import sys
        state = json.load(sys.stdin)
        print(json.dumps({"output": state["goal"]}))
        """,
    )
    evaluator = passing_evaluator(tmp_path / "eval.py")

    report = run_command_loop(
        goal="container wrap",
        max_iterations=1,
        work_command=[sys.executable, str(worker)],
        eval_command=[sys.executable, str(evaluator)],
        container={"runtime": str(runtime), "image": "python:3.11-slim", "network": "none", "read_only": True},
        allowed_commands=[Path(sys.executable).name],
    )

    assert report.success is True
    logged = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(logged) == 2
    assert all(argv[:5] == ["run", "--rm", "-i", "--network", "none"] for argv in logged)
    assert all("--read-only" in argv for argv in logged)
    assert all("python:3.11-slim" in argv for argv in logged)


def test_examples_include_multiple_public_loop_specs() -> None:
    example_specs = sorted(Path("examples").glob("*_loop.json"))
    assert {path.name for path in example_specs} >= {
        "quality_gate_loop.json",
        "redaction_loop.json",
        "allowlist_loop.json",
        "container_loop.json",
    }
