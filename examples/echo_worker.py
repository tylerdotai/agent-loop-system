import json
import sys
from typing import Any


def main() -> int:
    state: dict[str, Any] = json.load(sys.stdin)
    context = state.get("context", {})
    message = str(context.get("message", state.get("goal", "")))
    print(json.dumps({"output": message, "metadata": {"attempt": state.get("attempt")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
