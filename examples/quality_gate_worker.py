import json
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_CAPTURE_CHARS = 12000


def main() -> int:
    state: dict[str, Any] = json.load(sys.stdin)
    context = state.get("context", {})
    command = context.get("command")
    cwd = context.get("cwd", ".")
    timeout_seconds = int(context.get("command_timeout_seconds", 120))

    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError("context.command must be a non-empty list of strings")
    if timeout_seconds < 1:
        raise ValueError("context.command_timeout_seconds must be positive")

    completed = subprocess.run(
        command,
        cwd=Path(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = (completed.stdout + completed.stderr)[-MAX_CAPTURE_CHARS:]
    print(
        json.dumps(
            {
                "output": output,
                "metadata": {
                    "returncode": completed.returncode,
                    "command": command,
                    "cwd": str(cwd),
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
