import json
import sys
from pathlib import Path

from agent_loop.cli import main


def test_cli_runs_spec_file_and_prints_report(tmp_path: Path, capsys):
    worker = tmp_path / "worker.py"
    worker.write_text(
        """
import json
import sys
state = json.load(sys.stdin)
print(json.dumps({"output": "done" if state["attempt"] == 1 else "late"}))
""".strip()
    )
    evaluator = tmp_path / "evaluator.py"
    evaluator.write_text(
        """
import json
import sys
payload = json.load(sys.stdin)
print(json.dumps({"passed": payload["result"]["output"] == "done", "message": "ok"}))
""".strip()
    )
    spec = tmp_path / "loop.json"
    spec.write_text(
        json.dumps(
            {
                "goal": "cli contract",
                "max_iterations": 2,
                "work_command": [sys.executable, str(worker)],
                "eval_command": [sys.executable, str(evaluator)],
            }
        )
    )

    exit_code = main([str(spec)])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["success"] is True
    assert report["iterations"] == 1
