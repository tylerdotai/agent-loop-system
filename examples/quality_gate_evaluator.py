import json
import sys
from typing import Any


def main() -> int:
    payload: dict[str, Any] = json.load(sys.stdin)
    result = payload["result"]
    metadata = result.get("metadata", {})
    returncode = metadata.get("returncode")
    passed = returncode == 0
    message = "quality gate passed" if passed else f"quality gate failed with exit code {returncode}"
    print(json.dumps({"passed": passed, "message": message}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
