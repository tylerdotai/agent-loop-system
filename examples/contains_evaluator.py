import json
import sys
from typing import Any


def main() -> int:
    payload: dict[str, Any] = json.load(sys.stdin)
    context = payload.get("state", {}).get("context", {})
    expected = str(context.get("expected", ""))
    output = str(payload.get("result", {}).get("output", ""))
    passed = expected in output
    message = "expected text found" if passed else f"expected text not found: {expected}"
    print(json.dumps({"passed": passed, "message": message}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
