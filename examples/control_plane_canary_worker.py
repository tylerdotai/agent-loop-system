from __future__ import annotations

import base64
import json
import sys

from agent_loop.worker_api import control_call


def main() -> int:
    request = json.load(sys.stdin)
    control = request.get("control", {})
    socket_path = control.get("socket_path")
    token = control.get("token")
    if not isinstance(socket_path, str) or not isinstance(token, str):
        raise ValueError("canary worker requires scoped control credentials")

    control_call(
        socket_path,
        token,
        "heartbeat",
        {"lease_seconds": 30},
    )
    message = control_call(
        socket_path,
        token,
        "message.publish",
        {
            "topic": f"mission.{request['mission_id']}.canary",
            "kind": "checkpoint",
            "body": "canary worker reached the governed message board",
            "dedupe_key": f"{request['run_id']}:canary-checkpoint",
        },
    )
    artifact_content = json.dumps(
        {
            "mission_id": request["mission_id"],
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "message_id": message["message_id"],
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    artifact = control_call(
        socket_path,
        token,
        "artifact.put",
        {
            "filename": "canary-evidence.json",
            "media_type": "application/json",
            "content_base64": base64.b64encode(artifact_content).decode("ascii"),
            "dedupe_key": f"{request['run_id']}:canary-evidence",
        },
    )

    print(
        json.dumps(
            {
                "outcome": "candidate_complete",
                "summary": "canary completed through the scoped worker API",
                "artifact_ids": [artifact["artifact_id"]],
                "evidence": [
                    {
                        "kind": "command",
                        "value": "heartbeat, message, and artifact round trip",
                        "exit_code": 0,
                    }
                ],
                "fact_proposals": [],
                "residual_risks": [],
                "requested_followups": [],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
