from __future__ import annotations

import json

import pytest

from agent_loop.compact_protocol import (
    CompactProtocolError,
    Packet,
    compact_mapping,
    compile_symbolic_packet,
    decode_evidence_packet,
    decode_result_packet,
    encode_evidence_packet,
    encode_result_packet,
    expand_mapping,
    make_record,
    parse_packet,
    validate_action_intent,
    validate_metrics,
    validate_observation,
    validate_tom_request,
    validate_tom_result,
)


def test_packet_round_trip_is_canonical_and_escapes_variable_text() -> None:
    packet = Packet(
        "CTX",
        (
            make_record("Q", domain="SYS", query="audit|repository\nnow"),
            make_record("B", name="mode", value="read only"),
            make_record("C", name="prior", value={"count": 2, "summary": "bounded"}),
        ),
    )

    encoded = packet.encode()

    assert encoded == (
        "ACS1|CTX\n"
        "Q|SYS|audit\\|repository\\nnow\n"
        "B|mode|read only\n"
        'C|prior|{"count":2,"summary":"bounded"}'
    )
    assert parse_packet(encoded) == packet
    assert parse_packet(encoded).encode() == encoded


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("ACS2|CTX\nQ|SYS|work", "header"),
        ("ACS1|CTX\nX|unknown", "record code"),
        ("ACS1|INT\nB|mode|read only", "exactly one A"),
        ("ACS1|CTX\nQ|SYS|bad\\xescape", "escape"),
        ('ACS1|CTX\nC|prior|{"x":1,"x":2}', "duplicate JSON key"),
    ],
)
def test_parser_fails_closed_on_noncanonical_packets(payload: str, message: str) -> None:
    with pytest.raises(CompactProtocolError, match=message):
        parse_packet(payload)


def test_parser_rejects_oversized_packets() -> None:
    payload = "ACS1|CTX\nQ|SYS|" + "x" * 24_000

    with pytest.raises(CompactProtocolError, match="maximum size"):
        parse_packet(payload)


def test_evidence_packet_deduplicates_paths_and_uses_compact_ids() -> None:
    catalog = {
        "E0001": ("SECURITY.md", "Workers have no database access."),
        "E0002": ("SECURITY.md", "Workers have no provider credentials."),
        "E0003": ("README.md", "The control plane is authoritative."),
    }

    encoded, wire_catalog = encode_evidence_packet(catalog, domain="AUD", query="security")

    assert encoded.splitlines() == [
        "ACS1|CTX",
        "Q|AUD|security",
        "P|0|SECURITY.md",
        "P|1|README.md",
        "E|1|0|Workers have no database access.",
        "E|2|0|Workers have no provider credentials.",
        "E|3|1|The control plane is authoritative.",
    ]
    assert wire_catalog == {
        "1": ("SECURITY.md", "Workers have no database access."),
        "2": ("SECURITY.md", "Workers have no provider credentials."),
        "3": ("README.md", "The control plane is authoritative."),
    }
    assert decode_evidence_packet(parse_packet(encoded)) == wire_catalog


def test_compact_result_round_trip_preserves_canonical_object() -> None:
    result = {
        "role": "security",
        "summary": "No unsupported state changes.",
        "findings": [
            {
                "severity": "low",
                "file": "SECURITY.md",
                "evidence": "read only",
                "finding": "Boundary is explicit.",
                "recommendation": "Keep the gate.",
            }
        ],
        "artifact_ids": ["art_1"],
    }

    encoded = encode_result_packet(result)

    assert decode_result_packet(parse_packet(encoded)) == result
    assert len(encoded) < len(json.dumps(result, separators=(",", ":")))


def test_mapping_aliases_are_versioned_reversible_and_collision_safe() -> None:
    value = {"mission_id": "mis_1", "task_id": "tsk_1", "summary": "ok"}

    compact = compact_mapping(value)

    assert compact == {"m": "mis_1", "t": "tsk_1", "s": "ok"}
    assert expand_mapping(compact) == value
    with pytest.raises(CompactProtocolError, match="alias collision"):
        compact_mapping({"mission_id": "mis_1", "m": "spoof"})


def test_json_fields_are_canonical_snapshots_not_mutable_caller_objects() -> None:
    arguments = {"units": 2}
    record = make_record(
        "A",
        intent_id="intent-1",
        tick=7,
        ledger_version=12,
        action="move",
        target="node-b",
        arguments=arguments,
    )
    packet = Packet("INT", (record,))
    arguments["units"] = 99
    exposed = record.fields()["arguments"]
    exposed["units"] = 50

    assert '"units":2' in packet.encode()
    assert record.fields()["arguments"] == {"units": 2}


def test_action_intent_is_fenced_and_actor_comes_from_authenticated_envelope() -> None:
    packet = Packet(
        "INT",
        (
            make_record(
                "A",
                intent_id="intent-1",
                tick=7,
                ledger_version=12,
                action="move",
                target="node-b",
                arguments={"units": 2},
            ),
        ),
    )

    intent = validate_action_intent(
        packet,
        authenticated_agent_id="agent-a",
        expected_tick=7,
        expected_ledger_version=12,
        allowed_actions={"move", "wait"},
    )

    assert intent == {
        "intent_id": "intent-1",
        "tick": 7,
        "ledger_version": 12,
        "agent_id": "agent-a",
        "action": "move",
        "target": "node-b",
        "arguments": {"units": 2},
    }
    with pytest.raises(CompactProtocolError, match="stale ledger version"):
        validate_action_intent(
            packet,
            authenticated_agent_id="agent-a",
            expected_tick=7,
            expected_ledger_version=13,
            allowed_actions={"move"},
        )
    with pytest.raises(CompactProtocolError, match="not allowed"):
        validate_action_intent(
            packet,
            authenticated_agent_id="agent-a",
            expected_tick=7,
            expected_ledger_version=12,
            allowed_actions={"wait"},
        )


def test_observation_is_bound_to_authoritative_ledger_checkpoint() -> None:
    packet = Packet(
        "OBS",
        (
            make_record("L", tick=8, ledger_version=13, state_hash="a" * 64, event_ref="evt-13"),
            make_record(
                "O",
                observation_id="obs-1",
                tick=8,
                ledger_version=13,
                scope="local",
                value={"stock": 4},
            ),
        ),
    )

    observation = validate_observation(
        packet,
        expected_tick=8,
        expected_ledger_version=13,
        expected_state_hash="a" * 64,
        expected_event_ref="evt-13",
    )

    assert observation["ledger"]["state_hash"] == "a" * 64
    assert observation["observations"][0]["value"] == {"stock": 4}
    with pytest.raises(CompactProtocolError, match="state hash"):
        validate_observation(
            packet,
            expected_tick=8,
            expected_ledger_version=13,
            expected_state_hash="b" * 64,
            expected_event_ref="evt-13",
        )


def test_tom_request_is_explicit_bounded_and_contains_no_private_reasoning_field() -> None:
    packet = Packet(
        "TOM",
        (
            make_record(
                "K",
                request_id="tom-1",
                tick=8,
                ledger_version=13,
                target_agent="agent-b",
                level=2,
                budget_units=1,
                question="expected counter move",
            ),
        ),
    )

    request = validate_tom_request(
        packet,
        authenticated_agent_id="agent-a",
        expected_tick=8,
        expected_ledger_version=13,
        maximum_level=2,
        remaining_budget_units=1,
    )

    assert request["agent_id"] == "agent-a"
    assert request["level"] == 2
    assert not {"rationale", "trace", "chain_of_thought"}.intersection(request)
    with pytest.raises(CompactProtocolError, match="level exceeds"):
        validate_tom_request(
            packet,
            authenticated_agent_id="agent-a",
            expected_tick=8,
            expected_ledger_version=13,
            maximum_level=1,
            remaining_budget_units=1,
        )


def test_metric_packet_is_bounded_data_not_executable_logic() -> None:
    packet = Packet(
        "MET",
        (
            make_record(
                "M",
                tick=10,
                name="semantic_convergence",
                scope="population",
                value={"mean_cosine": 0.82},
                evidence_ref="artifact-10",
            ),
        ),
    )

    encoded = packet.encode()

    assert parse_packet(encoded) == packet
    assert validate_metrics(
        packet,
        expected_tick=10,
        allowed_metrics={"semantic_convergence", "action_entropy"},
        allowed_evidence_refs={"artifact-10"},
    ) == [
        {
            "tick": 10,
            "name": "semantic_convergence",
            "scope": "population",
            "value": {"mean_cosine": 0.82},
            "evidence_ref": "artifact-10",
        }
    ]
    with pytest.raises(CompactProtocolError, match="record code"):
        parse_packet("ACS1|MET\nEXEC|shell|whoami")


def test_symbolic_authoring_aliases_compile_to_canonical_wire() -> None:
    source = "ACS1-SRC|INT\n" + '[>]ACT|A|intent-1|7|12|move|node-b|{"units":2}'

    packet = compile_symbolic_packet(source)

    assert packet.encode() == "ACS1|INT\n" + 'A|intent-1|7|12|move|node-b|{"units":2}'
    with pytest.raises(CompactProtocolError, match="does not permit record"):
        compile_symbolic_packet(source.replace("[>]ACT", "[?]ACT"))


def test_tom_results_are_typed_and_reject_private_reasoning_fields() -> None:
    packet = Packet(
        "TMR",
        (
            make_record(
                "Y",
                request_id="tom-1",
                status="SUCCEEDED",
                prediction={"move": "wait"},
                confidence=0.7,
                assumption_codes=["scarcity"],
                evidence_refs=["obs-1"],
                model_id="nemotron",
                model_version="3.5",
                template_version="tom-v1",
                output_schema_version="prediction-v1",
                charged_units=1,
                result_digest="b" * 64,
            ),
        ),
    )

    result = validate_tom_result(
        packet,
        expected_request_id="tom-1",
        reserved_budget_units=1,
        expected_charged_units=1,
        expected_model_id="nemotron",
        expected_model_version="3.5",
        expected_template_version="tom-v1",
        expected_output_schema_version="prediction-v1",
        allowed_assumption_codes={"scarcity"},
        allowed_evidence_refs={"obs-1"},
        expected_result_digest="b" * 64,
        prediction_validator=lambda value: isinstance(value, dict) and set(value) == {"move"},
    )

    assert result["status"] == "SUCCEEDED"
    with pytest.raises(CompactProtocolError, match="evidence reference"):
        validate_tom_result(
            packet,
            expected_request_id="tom-1",
            reserved_budget_units=1,
            expected_charged_units=1,
            expected_model_id="nemotron",
            expected_model_version="3.5",
            expected_template_version="tom-v1",
            expected_output_schema_version="prediction-v1",
            allowed_assumption_codes={"scarcity"},
            allowed_evidence_refs={"obs-2"},
            expected_result_digest="b" * 64,
            prediction_validator=lambda _value: True,
        )
    with pytest.raises(CompactProtocolError, match="private reasoning field"):
        Packet("RES", (make_record("Z", status="OK", result={"chain_of_thought": "secret"}),))
    with pytest.raises(CompactProtocolError, match="not allowed in RES"):
        Packet(
            "RES",
            (
                make_record("Z", status="OK", result={"summary": "ok"}),
                make_record(
                    "M",
                    tick=1,
                    name="attacker_metric",
                    scope="population",
                    value={"score": 1},
                    evidence_ref="forged",
                ),
            ),
        )


def test_reviewer_boundaries_are_fail_closed_and_capacity_safe() -> None:
    huge_integer = (
        "ACS1|INT\nA|intent-1|"
        + "9" * 5_000
        + '|12|move|node-b|{"units":2}'
    )
    with pytest.raises(CompactProtocolError, match="integer"):
        parse_packet(huge_integer)
    with pytest.raises(CompactProtocolError, match="evidence ID"):
        encode_evidence_packet(
            {"E" + "9" * 5_000: ("SECURITY.md", "bounded")},
            domain="AUD",
            query="security",
        )
    for private_key in (
        "Chain_Of_Thought",
        "analysis",
        "chain-of-thought",
        "debugTrace",
        "hiddenReasoning",
        "internalMonologue",
        "privateReasoning",
        "promptTranscript",
        "rawPrompt",
        "rawOutput",
        "rawResponse",
        "rationale",
        "reasoning",
        "reasoningTrace",
        "scratch-pad",
        "thoughts",
    ):
        with pytest.raises(CompactProtocolError, match="private reasoning field"):
            Packet("RES", (make_record("Z", status="OK", result={private_key: "secret"}),))
    with pytest.raises(CompactProtocolError, match="evidence ID"):
        encode_evidence_packet(
            {"E" + "0" * 5_000 + "1": ("SECURITY.md", "bounded")},
            domain="AUD",
            query="security",
        )

    catalog = {
        f"E{index:04d}": (f"src/file-{index}.py", f"evidence {index}")
        for index in range(1, 241)
    }
    encoded, wire_catalog = encode_evidence_packet(catalog, domain="AUD", query="security")
    assert len(wire_catalog) == 240
    assert decode_evidence_packet(parse_packet(encoded)) == wire_catalog

    value = {
        "role": "security",
        "provider_usage": {
            "u": 7,
            "nested": {"r": "raw"},
            "findings": [{"r": "opaque"}],
        },
    }
    compact = compact_mapping(value)
    assert compact == {
        "r": "security",
        "u": {
            "u": 7,
            "nested": {"r": "raw"},
            "findings": [{"r": "opaque"}],
        },
    }
    assert expand_mapping(compact) == value

    source = "ACS1-SRC|INT\n" + '[>]ACT|A|intent-1|7|12|move|node-b|{"units":2}'
    with pytest.raises(CompactProtocolError, match="domain SYS does not permit"):
        compile_symbolic_packet(source.replace("[>]ACT", "[>]SYS"))
