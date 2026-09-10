from __future__ import annotations

import tomllib
from pathlib import Path

import agent_loop


def test_top_level_api_exports_runner_agnostic_control_plane() -> None:
    expected = {
        "ActionBroker",
        "ArtifactStore",
        "BudgetService",
        "CompactProtocolError",
        "Coordinator",
        "JsonSubprocessRunner",
        "LoopEngine",
        "MessageBoard",
        "ModelBroker",
        "ModelBrokerPolicy",
        "Packet",
        "Record",
        "SQLiteStore",
        "WorkflowService",
        "WorkerAPI",
        "compile_symbolic_packet",
        "make_record",
        "parse_packet",
        "validate_action_intent",
        "validate_metrics",
        "validate_observation",
        "validate_tom_request",
        "validate_tom_result",
    }

    assert expected <= set(agent_loop.__all__)
    for name in expected:
        assert getattr(agent_loop, name) is not None


def test_runner_specific_autonomy_adapter_is_not_in_default_namespace() -> None:
    assert "AutonomyController" not in agent_loop.__all__
    assert "AgentProvisioner" not in agent_loop.__all__


def test_control_plane_has_an_installed_cli_entrypoint() -> None:
    metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())

    assert metadata["project"]["scripts"]["agent-loop-control"] == "agent_loop.control_cli:main"
    assert metadata["project"]["scripts"]["agent-loop-worker"] == "agent_loop.worker_cli:main"
    assert metadata["project"]["scripts"]["agent-loop-model-broker"] == (
        "agent_loop.model_broker_cli:main"
    )
