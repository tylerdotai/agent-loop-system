from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


class CompactProtocolError(ValueError):
    """Raised when an ACS1 packet is malformed or violates its typed contract."""


PROTOCOL_VERSION = "ACS1"
MAX_PACKET_CHARS = 24_000
MAX_RECORDS = 512
MAX_FIELD_CHARS = 16_000
MAX_JSON_DEPTH = 6
MAX_JSON_ITEMS = 128
MAX_INTEGER = 9_223_372_036_854_775_807

_FIELD_SCHEMAS: dict[str, tuple[tuple[str, str], ...]] = {
    "Q": (("domain", "text"), ("query", "text")),
    "B": (("name", "text"), ("value", "text")),
    "R": (("kind", "text"), ("identifier", "text"), ("version", "text")),
    "S": (("phase", "text"), ("status", "text"), ("evidence_ref", "text")),
    "D": (("source", "text"), ("target", "text")),
    "C": (("name", "text"), ("value", "json")),
    "T": (("name", "text"), ("text", "text")),
    "P": (("path_id", "text"), ("path", "text")),
    "E": (("evidence_id", "text"), ("path_id", "text"), ("text", "text")),
    "A": (
        ("intent_id", "text"),
        ("tick", "int"),
        ("ledger_version", "int"),
        ("action", "text"),
        ("target", "text"),
        ("arguments", "json"),
    ),
    "O": (
        ("observation_id", "text"),
        ("tick", "int"),
        ("ledger_version", "int"),
        ("scope", "text"),
        ("value", "json"),
    ),
    "L": (
        ("tick", "int"),
        ("ledger_version", "int"),
        ("state_hash", "text"),
        ("event_ref", "text"),
    ),
    "K": (
        ("request_id", "text"),
        ("tick", "int"),
        ("ledger_version", "int"),
        ("target_agent", "text"),
        ("level", "int"),
        ("budget_units", "int"),
        ("question", "text"),
    ),
    "M": (
        ("tick", "int"),
        ("name", "text"),
        ("scope", "text"),
        ("value", "json"),
        ("evidence_ref", "text"),
    ),
    "Y": (
        ("request_id", "text"),
        ("status", "text"),
        ("prediction", "json"),
        ("confidence", "json"),
        ("assumption_codes", "json"),
        ("evidence_refs", "json"),
        ("model_id", "text"),
        ("model_version", "text"),
        ("template_version", "text"),
        ("output_schema_version", "text"),
        ("charged_units", "int"),
        ("result_digest", "text"),
    ),
    "Z": (("status", "text"), ("result", "json")),
}

_ALLOWED_RECORDS: dict[str, frozenset[str]] = {
    "CTX": frozenset({"Q", "B", "R", "S", "D", "C", "T", "P", "E"}),
    "INT": frozenset({"A", "R", "B"}),
    "OBS": frozenset({"O", "L", "R"}),
    "RES": frozenset({"Z", "R"}),
    "TOM": frozenset({"K", "R", "B"}),
    "TMR": frozenset({"Y", "R"}),
    "MET": frozenset({"M", "R"}),
}

_KEY_ALIASES = {
    "mission_id": "m",
    "task_id": "t",
    "run_id": "n",
    "worker_id": "w",
    "role": "r",
    "model": "o",
    "provider_usage": "u",
    "summary": "s",
    "audit_areas": "a",
    "findings": "f",
    "severity": "v",
    "file": "p",
    "evidence": "e",
    "finding": "d",
    "recommendation": "x",
    "executive_summary": "q",
    "overall_risk": "k",
    "verdict": "z",
    "issues": "i",
    "artifact_ids": "h",
    "deterministic_evidence_check": "c",
    "verified_count": "y",
}
_REVERSE_ALIASES = {value: key for key, value in _KEY_ALIASES.items()}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_JSON_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_STATE_HASH = re.compile(r"^[a-f0-9]{64}$")
_PRIVATE_REASONING_KEYS = frozenset(
    {
        "analysis",
        "chain_of_thought",
        "chainofthought",
        "debug_trace",
        "hidden_reasoning",
        "internal_monologue",
        "private_reasoning",
        "prompt_transcript",
        "raw_prompt",
        "raw_output",
        "raw_response",
        "rationale",
        "reasoning",
        "reasoning_trace",
        "scratchpad",
        "thoughts",
    }
)
_PRIVATE_REASONING_KEYS_COLLAPSED = frozenset(
    key.replace("_", "") for key in _PRIVATE_REASONING_KEYS
)
_SOURCE_RECORD = re.compile(
    r"^(\[\?\]|\[!\]|\[@\]|\[\.\]|\[>\]|\[=\]|\[∴\])([A-Z]{3})\|(.*)$"
)
_SOURCE_DOMAINS = frozenset(
    {"SYS", "EXP", "OPT", "BUG", "VAL", "DEP", "SIM", "OBS", "ACT", "LED", "TOM", "MET"}
)
_SYMBOL_RECORDS = {
    "[?]": frozenset({"Q"}),
    "[!]": frozenset({"B"}),
    "[@]": frozenset({"R", "P", "E", "O", "L"}),
    "[.]": frozenset({"S", "K", "M"}),
    "[>]": frozenset({"D", "A"}),
    "[=]": frozenset({"Z", "Y"}),
    "[∴]": frozenset({"Z", "Y"}),
}
_DOMAIN_RECORDS = {
    "SYS": frozenset({"Q", "B", "R", "S", "C", "T"}),
    "EXP": frozenset({"Q", "S", "D"}),
    "OPT": frozenset({"Q", "B", "S", "M"}),
    "BUG": frozenset({"Q", "R", "S", "E"}),
    "VAL": frozenset({"Q", "R", "S", "E", "Z"}),
    "DEP": frozenset({"R", "D"}),
    "SIM": frozenset({"Q", "B", "C", "T"}),
    "OBS": frozenset({"O", "R"}),
    "ACT": frozenset({"A", "D"}),
    "LED": frozenset({"L", "R"}),
    "TOM": frozenset({"K", "Y", "R", "B"}),
    "MET": frozenset({"M", "R"}),
}


@dataclass(frozen=True)
class _CanonicalJSON:
    encoded: str


@dataclass(frozen=True)
class Record:
    code: str
    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        schema = _FIELD_SCHEMAS.get(self.code)
        if schema is None:
            raise CompactProtocolError(f"unknown record code: {self.code}")
        if len(self.values) != len(schema):
            raise CompactProtocolError(
                f"record {self.code} requires {len(schema)} fields, got {len(self.values)}"
            )
        normalized = tuple(
            _normalize_field(value, field_type, field_name)
            for value, (field_name, field_type) in zip(self.values, schema, strict=True)
        )
        object.__setattr__(self, "values", normalized)

    def fields(self) -> dict[str, Any]:
        return {
            name: _public_field(value, field_type)
            for value, (name, field_type) in zip(
                self.values,
                _FIELD_SCHEMAS[self.code],
                strict=True,
            )
        }

    def encode(self) -> str:
        fields = [self.code]
        for value, (_name, field_type) in zip(
            self.values,
            _FIELD_SCHEMAS[self.code],
            strict=True,
        ):
            if field_type == "json":
                if not isinstance(value, _CanonicalJSON):
                    raise AssertionError("JSON field was not canonicalized")
                rendered = value.encoded
            else:
                rendered = str(value)
            fields.append(_escape(rendered))
        return "|".join(fields)


@dataclass(frozen=True)
class Packet:
    kind: str
    records: tuple[Record, ...]

    def __post_init__(self) -> None:
        if self.kind not in _ALLOWED_RECORDS:
            raise CompactProtocolError(f"unknown packet kind: {self.kind}")
        records = tuple(self.records)
        object.__setattr__(self, "records", records)
        if not 1 <= len(records) <= MAX_RECORDS:
            raise CompactProtocolError("packet must contain a bounded non-empty record set")
        allowed = _ALLOWED_RECORDS[self.kind]
        for record in records:
            if record.code not in allowed:
                raise CompactProtocolError(
                    f"record code {record.code} is not allowed in {self.kind} packets"
                )
        counts = {code: sum(record.code == code for record in records) for code in _FIELD_SCHEMAS}
        if self.kind == "INT" and counts["A"] != 1:
            raise CompactProtocolError("INT packet requires exactly one A record")
        if self.kind == "OBS" and (counts["L"] != 1 or counts["O"] < 1):
            raise CompactProtocolError("OBS packet requires exactly one L and at least one O record")
        if self.kind == "RES" and counts["Z"] != 1:
            raise CompactProtocolError("RES packet requires exactly one Z record")
        if self.kind == "TOM" and counts["K"] != 1:
            raise CompactProtocolError("TOM packet requires exactly one K record")
        if self.kind == "TMR" and counts["Y"] != 1:
            raise CompactProtocolError("TMR packet requires exactly one Y record")
        if self.kind == "MET" and counts["M"] < 1:
            raise CompactProtocolError("MET packet requires at least one M record")

    def encode(self) -> str:
        encoded = "\n".join((f"{PROTOCOL_VERSION}|{self.kind}", *(r.encode() for r in self.records)))
        if len(encoded) > MAX_PACKET_CHARS:
            raise CompactProtocolError("packet exceeds maximum size")
        return encoded


def make_record(code: str, **fields: Any) -> Record:
    schema = _FIELD_SCHEMAS.get(code)
    if schema is None:
        raise CompactProtocolError(f"unknown record code: {code}")
    expected = {name for name, _field_type in schema}
    if set(fields) != expected:
        raise CompactProtocolError(f"record {code} fields must be exactly {sorted(expected)}")
    return Record(code, tuple(fields[name] for name, _field_type in schema))


def parse_packet(payload: str) -> Packet:
    if not isinstance(payload, str) or not payload:
        raise CompactProtocolError("packet must be a non-empty string")
    if len(payload) > MAX_PACKET_CHARS:
        raise CompactProtocolError("packet exceeds maximum size")
    if payload.endswith("\n"):
        raise CompactProtocolError("packet must not contain a trailing newline")
    lines = payload.splitlines()
    if not lines or not lines[0].startswith(f"{PROTOCOL_VERSION}|"):
        raise CompactProtocolError("invalid ACS1 header")
    header = lines[0].split("|")
    if len(header) != 2 or header[0] != PROTOCOL_VERSION or header[1] not in _ALLOWED_RECORDS:
        raise CompactProtocolError("invalid ACS1 header")
    if not 1 <= len(lines) - 1 <= MAX_RECORDS:
        raise CompactProtocolError("packet must contain a bounded non-empty record set")
    records: list[Record] = []
    for line in lines[1:]:
        fields = _split_escaped(line)
        code = fields[0] if fields else ""
        schema = _FIELD_SCHEMAS.get(code)
        if schema is None:
            raise CompactProtocolError(f"unknown record code: {code}")
        if len(fields) - 1 != len(schema):
            raise CompactProtocolError(
                f"record {code} requires {len(schema)} fields, got {len(fields) - 1}"
            )
        values = tuple(
            _parse_field(value, field_type, field_name)
            for value, (field_name, field_type) in zip(fields[1:], schema, strict=True)
        )
        records.append(Record(code, values))
    packet = Packet(header[1], tuple(records))
    if packet.encode() != payload:
        raise CompactProtocolError("packet is not in canonical form")
    return packet


def compile_symbolic_packet(source: str) -> Packet:
    if not isinstance(source, str) or not source:
        raise CompactProtocolError("symbolic source must be a non-empty string")
    if len(source) > MAX_PACKET_CHARS:
        raise CompactProtocolError("symbolic source exceeds maximum size")
    if source.endswith("\n"):
        raise CompactProtocolError("symbolic source must not contain a trailing newline")
    lines = source.splitlines()
    header = lines[0].split("|") if lines else []
    if len(header) != 2 or header[0] != "ACS1-SRC" or header[1] not in _ALLOWED_RECORDS:
        raise CompactProtocolError("invalid ACS1-SRC header")
    canonical = [f"ACS1|{header[1]}"]
    for line in lines[1:]:
        match = _SOURCE_RECORD.fullmatch(line)
        if match is None:
            raise CompactProtocolError("invalid symbolic source record")
        symbol, domain, body = match.groups()
        if domain not in _SOURCE_DOMAINS:
            raise CompactProtocolError(f"unknown symbolic domain: {domain}")
        fields = _split_escaped(body)
        code = fields[0] if fields else ""
        if code not in _SYMBOL_RECORDS[symbol]:
            raise CompactProtocolError(f"symbol {symbol} does not permit record {code}")
        if code not in _DOMAIN_RECORDS[domain]:
            raise CompactProtocolError(f"domain {domain} does not permit record {code}")
        canonical.append(body)
    return parse_packet("\n".join(canonical))


def compact_mapping(value: Any) -> Any:
    return _map_result_keys(value, expand=False, aliased=True)


def expand_mapping(value: Any) -> Any:
    return _map_result_keys(value, expand=True, aliased=True)


def encode_result_packet(result: Mapping[str, Any]) -> str:
    if not isinstance(result, Mapping):
        raise CompactProtocolError("result must be an object")
    return Packet(
        "RES",
        (make_record("Z", status="OK", result=compact_mapping(dict(result))),),
    ).encode()


def decode_result_packet(packet: Packet) -> dict[str, Any]:
    if packet.kind != "RES":
        raise CompactProtocolError("result packet must have RES kind")
    record = next(record for record in packet.records if record.code == "Z")
    fields = record.fields()
    if fields["status"] != "OK" or not isinstance(fields["result"], dict):
        raise CompactProtocolError("result packet must contain one successful object result")
    expanded = expand_mapping(fields["result"])
    if not isinstance(expanded, dict):
        raise CompactProtocolError("expanded result must be an object")
    return expanded


def encode_evidence_packet(
    catalog: Mapping[str, tuple[str, str]],
    *,
    domain: str,
    query: str,
) -> tuple[str, dict[str, tuple[str, str]]]:
    if not catalog:
        raise CompactProtocolError("evidence catalog must not be empty")
    paths: dict[str, str] = {}
    path_records: list[Record] = []
    evidence_rows: list[tuple[str, str, str]] = []
    wire_catalog: dict[str, tuple[str, str]] = {}
    for canonical_id, item in catalog.items():
        if (
            not isinstance(canonical_id, str)
            or not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
        ):
            raise CompactProtocolError("evidence catalog entries are invalid")
        path, text = item
        if path not in paths:
            path_id = _base36(len(paths))
            paths[path] = path_id
            path_records.append(make_record("P", path_id=path_id, path=path))
        wire_id = _compact_evidence_id(canonical_id)
        if wire_id in wire_catalog:
            raise CompactProtocolError("evidence ID compaction collision")
        wire_catalog[wire_id] = (path, text)
        evidence_rows.append((wire_id, paths[path], text))
    records = [make_record("Q", domain=domain, query=query), *path_records]
    records.extend(
        make_record("E", evidence_id=evidence_id, path_id=path_id, text=text)
        for evidence_id, path_id, text in evidence_rows
    )
    return Packet("CTX", tuple(records)).encode(), wire_catalog


def decode_evidence_packet(packet: Packet) -> dict[str, tuple[str, str]]:
    if packet.kind != "CTX":
        raise CompactProtocolError("evidence packet must have CTX kind")
    paths: dict[str, str] = {}
    catalog: dict[str, tuple[str, str]] = {}
    for record in packet.records:
        fields = record.fields()
        if record.code == "P":
            if fields["path_id"] in paths:
                raise CompactProtocolError("duplicate path ID")
            paths[fields["path_id"]] = fields["path"]
        elif record.code == "E":
            path = paths.get(fields["path_id"])
            if path is None:
                raise CompactProtocolError("evidence references an unknown path ID")
            if fields["evidence_id"] in catalog:
                raise CompactProtocolError("duplicate evidence ID")
            catalog[fields["evidence_id"]] = (path, fields["text"])
    if not catalog:
        raise CompactProtocolError("evidence packet contains no evidence")
    return catalog


def validate_action_intent(
    packet: Packet,
    *,
    authenticated_agent_id: str,
    expected_tick: int,
    expected_ledger_version: int,
    allowed_actions: set[str] | frozenset[str],
) -> dict[str, Any]:
    if packet.kind != "INT":
        raise CompactProtocolError("action intent must have INT kind")
    actor = _identifier(authenticated_agent_id, "authenticated agent ID")
    record = next(record for record in packet.records if record.code == "A")
    fields = record.fields()
    _require_fence(fields, expected_tick, expected_ledger_version)
    _identifier(fields["intent_id"], "intent ID")
    _identifier(fields["action"], "action")
    if fields["action"] not in allowed_actions:
        raise CompactProtocolError("action is not allowed by the mediator")
    if not isinstance(fields["arguments"], dict):
        raise CompactProtocolError("action arguments must be an object")
    return {**fields, "agent_id": actor}


def validate_observation(
    packet: Packet,
    *,
    expected_tick: int,
    expected_ledger_version: int,
    expected_state_hash: str,
    expected_event_ref: str,
) -> dict[str, Any]:
    if packet.kind != "OBS":
        raise CompactProtocolError("observation must have OBS kind")
    ledger = next(record.fields() for record in packet.records if record.code == "L")
    _require_fence(ledger, expected_tick, expected_ledger_version)
    if not _STATE_HASH.fullmatch(ledger["state_hash"]):
        raise CompactProtocolError("ledger state hash must be lowercase SHA-256")
    _identifier(ledger["event_ref"], "ledger event reference")
    if ledger["state_hash"] != expected_state_hash:
        raise CompactProtocolError("ledger state hash does not match authoritative checkpoint")
    if ledger["event_ref"] != expected_event_ref:
        raise CompactProtocolError("ledger event reference does not match authoritative checkpoint")
    observations = [record.fields() for record in packet.records if record.code == "O"]
    seen: set[str] = set()
    for observation in observations:
        _require_fence(observation, expected_tick, expected_ledger_version)
        observation_id = _identifier(observation["observation_id"], "observation ID")
        if observation_id in seen:
            raise CompactProtocolError("observation IDs must be unique")
        seen.add(observation_id)
    return {"ledger": ledger, "observations": observations}


def validate_tom_request(
    packet: Packet,
    *,
    authenticated_agent_id: str,
    expected_tick: int,
    expected_ledger_version: int,
    maximum_level: int,
    remaining_budget_units: int,
) -> dict[str, Any]:
    if packet.kind != "TOM":
        raise CompactProtocolError("theory-of-mind request must have TOM kind")
    actor = _identifier(authenticated_agent_id, "authenticated agent ID")
    fields = next(record.fields() for record in packet.records if record.code == "K")
    _require_fence(fields, expected_tick, expected_ledger_version)
    _identifier(fields["request_id"], "theory-of-mind request ID")
    _identifier(fields["target_agent"], "theory-of-mind target agent")
    if fields["level"] > maximum_level:
        raise CompactProtocolError("theory-of-mind level exceeds policy")
    if fields["budget_units"] < 1 or fields["budget_units"] > remaining_budget_units:
        raise CompactProtocolError("theory-of-mind budget is unavailable")
    return {**fields, "agent_id": actor}


def validate_metrics(
    packet: Packet,
    *,
    expected_tick: int,
    allowed_metrics: set[str] | frozenset[str],
    allowed_evidence_refs: set[str] | frozenset[str],
) -> list[dict[str, Any]]:
    if packet.kind != "MET":
        raise CompactProtocolError("metrics must have MET kind")
    if not allowed_metrics:
        raise CompactProtocolError("metric allowlist must not be empty")
    metrics = [record.fields() for record in packet.records if record.code == "M"]
    for metric in metrics:
        if metric["tick"] != expected_tick:
            raise CompactProtocolError("stale metric tick")
        _identifier(metric["name"], "metric name")
        _identifier(metric["evidence_ref"], "metric evidence reference")
        if metric["name"] not in allowed_metrics:
            raise CompactProtocolError("metric is not allowed by policy")
        if metric["evidence_ref"] not in allowed_evidence_refs:
            raise CompactProtocolError("metric evidence reference is not authoritative")
    return metrics


def validate_tom_result(
    packet: Packet,
    *,
    expected_request_id: str,
    reserved_budget_units: int,
    expected_charged_units: int,
    expected_model_id: str,
    expected_model_version: str,
    expected_template_version: str,
    expected_output_schema_version: str,
    allowed_assumption_codes: set[str] | frozenset[str],
    allowed_evidence_refs: set[str] | frozenset[str],
    expected_result_digest: str,
    prediction_validator: Callable[[Any], bool],
) -> dict[str, Any]:
    if packet.kind != "TMR":
        raise CompactProtocolError("theory-of-mind result must have TMR kind")
    if (
        isinstance(reserved_budget_units, bool)
        or not isinstance(reserved_budget_units, int)
        or reserved_budget_units < 1
    ):
        raise CompactProtocolError("reserved theory-of-mind budget must be positive")
    fields = next(record.fields() for record in packet.records if record.code == "Y")
    request_id = _identifier(fields["request_id"], "theory-of-mind request ID")
    if request_id != expected_request_id:
        raise CompactProtocolError("theory-of-mind result request ID mismatch")
    if fields["status"] not in {"SUCCEEDED", "REJECTED", "TIMED_OUT", "ERROR"}:
        raise CompactProtocolError("theory-of-mind result status is invalid")
    if fields["status"] == "SUCCEEDED" and fields["prediction"] is None:
        raise CompactProtocolError("successful theory-of-mind result requires a prediction")
    expected_provenance = {
        "model_id": expected_model_id,
        "model_version": expected_model_version,
        "template_version": expected_template_version,
        "output_schema_version": expected_output_schema_version,
    }
    for field_name, expected_value in expected_provenance.items():
        _identifier(expected_value, f"expected theory-of-mind {field_name}")
        if fields[field_name] != expected_value:
            raise CompactProtocolError(f"theory-of-mind {field_name} mismatch")
    for field_name in ("assumption_codes", "evidence_refs"):
        values = fields[field_name]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise CompactProtocolError(f"theory-of-mind {field_name} must be a string array")
        for value in values:
            _identifier(value, f"theory-of-mind {field_name} value")
    if not set(fields["assumption_codes"]).issubset(allowed_assumption_codes):
        raise CompactProtocolError("theory-of-mind assumption code is not allowed")
    if not set(fields["evidence_refs"]).issubset(allowed_evidence_refs):
        raise CompactProtocolError("theory-of-mind evidence reference is not authoritative")
    if (
        isinstance(expected_charged_units, bool)
        or not isinstance(expected_charged_units, int)
        or expected_charged_units < 0
        or expected_charged_units > reserved_budget_units
    ):
        raise CompactProtocolError("expected theory-of-mind charge is invalid")
    if fields["charged_units"] != expected_charged_units:
        raise CompactProtocolError("theory-of-mind charged units mismatch")
    if fields["status"] == "SUCCEEDED" and fields["charged_units"] < 1:
        raise CompactProtocolError("successful theory-of-mind result requires a charge")
    if fields["charged_units"] > reserved_budget_units:
        raise CompactProtocolError("theory-of-mind result exceeds reserved budget")
    if not _STATE_HASH.fullmatch(fields["result_digest"]):
        raise CompactProtocolError("theory-of-mind result digest must be lowercase SHA-256")
    if not _STATE_HASH.fullmatch(expected_result_digest):
        raise CompactProtocolError("expected theory-of-mind result digest is invalid")
    if fields["result_digest"] != expected_result_digest:
        raise CompactProtocolError("theory-of-mind result digest mismatch")
    try:
        prediction_valid = prediction_validator(fields["prediction"])
    except Exception as exc:
        raise CompactProtocolError("theory-of-mind prediction validation failed") from exc
    if not prediction_valid:
        raise CompactProtocolError("theory-of-mind prediction violates its schema")
    return fields


def _require_fence(fields: Mapping[str, Any], tick: int, ledger_version: int) -> None:
    if fields.get("tick") != tick:
        raise CompactProtocolError("stale simulation tick")
    if fields.get("ledger_version") != ledger_version:
        raise CompactProtocolError("stale ledger version")


def _normalize_field(value: Any, field_type: str, name: str) -> Any:
    if field_type == "text":
        if not isinstance(value, str) or not value or len(value) > MAX_FIELD_CHARS:
            raise CompactProtocolError(f"{name} must be a non-empty bounded string")
        if "\x00" in value or any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise CompactProtocolError(f"{name} contains a forbidden control character")
        return value
    if field_type == "int":
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_INTEGER
        ):
            raise CompactProtocolError(f"{name} must be a non-negative integer")
        return value
    if field_type == "json":
        if isinstance(value, _CanonicalJSON):
            return value
        _validate_json(value, depth=0)
        return _CanonicalJSON(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
    raise AssertionError(f"unknown field type: {field_type}")


def _parse_field(value: str, field_type: str, name: str) -> Any:
    if field_type == "text":
        return _normalize_field(value, field_type, name)
    if field_type == "int":
        if len(value) > 19 or not _INTEGER.fullmatch(value):
            raise CompactProtocolError(f"{name} must use canonical integer syntax")
        try:
            parsed_integer = int(value)
        except ValueError as exc:
            raise CompactProtocolError(f"{name} must use canonical integer syntax") from exc
        return _normalize_field(parsed_integer, field_type, name)
    if field_type == "json":
        try:
            parsed = json.loads(
                value,
                object_pairs_hook=_unique_object,
                parse_int=_parse_json_integer,
                parse_constant=lambda constant: (_raise_json_constant(constant)),
            )
        except (json.JSONDecodeError, CompactProtocolError) as exc:
            if isinstance(exc, CompactProtocolError):
                raise
            raise CompactProtocolError(f"{name} must contain canonical JSON") from exc
        _validate_json(parsed, depth=0)
        return parsed
    raise AssertionError(f"unknown field type: {field_type}")


def _public_field(value: Any, field_type: str) -> Any:
    if field_type != "json":
        return value
    if not isinstance(value, _CanonicalJSON):
        raise AssertionError("JSON field was not canonicalized")
    return json.loads(value.encoded, object_pairs_hook=_unique_object)


def _validate_json(value: Any, *, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise CompactProtocolError("JSON value exceeds maximum depth")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
            raise CompactProtocolError("JSON string exceeds maximum size")
        return
    if isinstance(value, int):
        if abs(value) > MAX_INTEGER:
            raise CompactProtocolError("JSON integer exceeds maximum magnitude")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CompactProtocolError("JSON number must be finite")
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_ITEMS:
            raise CompactProtocolError("JSON array exceeds maximum size")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_JSON_ITEMS:
            raise CompactProtocolError("JSON object exceeds maximum size")
        for key, item in value.items():
            if not isinstance(key, str) or not _JSON_KEY.fullmatch(key):
                raise CompactProtocolError("JSON object key is invalid")
            normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            if (
                normalized_key in _PRIVATE_REASONING_KEYS
                or normalized_key.replace("_", "") in _PRIVATE_REASONING_KEYS_COLLAPSED
            ):
                raise CompactProtocolError(f"private reasoning field is forbidden: {key}")
            _validate_json(item, depth=depth + 1)
        return
    raise CompactProtocolError("JSON value contains an unsupported type")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CompactProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _raise_json_constant(constant: str) -> None:
    raise CompactProtocolError(f"JSON constant is not allowed: {constant}")


def _parse_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 19:
        raise CompactProtocolError("JSON integer exceeds maximum magnitude")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CompactProtocolError("JSON integer is invalid") from exc
    if abs(parsed) > MAX_INTEGER:
        raise CompactProtocolError("JSON integer exceeds maximum magnitude")
    return parsed


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _split_escaped(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "|":
            fields.append("".join(current))
            current = []
        elif char == "\\":
            index += 1
            if index >= len(line):
                raise CompactProtocolError("invalid escape at end of record")
            escaped = line[index]
            replacements = {"\\": "\\", "|": "|", "n": "\n", "r": "\r", "t": "\t"}
            if escaped not in replacements:
                raise CompactProtocolError(f"invalid escape sequence: \\{escaped}")
            current.append(replacements[escaped])
        else:
            current.append(char)
        index += 1
    fields.append("".join(current))
    return fields


def _map_result_keys(value: Any, *, expand: bool, aliased: bool) -> Any:
    if isinstance(value, list):
        return [_map_result_keys(item, expand=expand, aliased=aliased) for item in value]
    if not isinstance(value, dict):
        return value
    aliases = _REVERSE_ALIASES if expand else _KEY_ALIASES
    mapped: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CompactProtocolError("mapping keys must be strings")
        if not expand and aliased and key in _REVERSE_ALIASES:
            raise CompactProtocolError(f"mapping alias collision: reserved alias {key}")
        target = aliases.get(key, key) if aliased else key
        if target in mapped:
            raise CompactProtocolError(f"mapping alias collision: {target}")
        canonical_key = target if expand else key
        child_aliased = aliased and canonical_key == "findings"
        mapped[target] = _map_result_keys(
            item,
            expand=expand,
            aliased=child_aliased,
        )
    _validate_json(mapped, depth=0)
    return mapped


def _compact_evidence_id(value: str) -> str:
    if len(value) > 64:
        raise CompactProtocolError("evidence ID exceeds maximum size")
    match = re.fullmatch(r"E0*([1-9][0-9]*)", value)
    if match is None:
        return value
    digits = match.group(1)
    if len(digits) > 19:
        raise CompactProtocolError("evidence ID numeric suffix exceeds maximum")
    try:
        numeric_id = int(digits)
    except ValueError as exc:
        raise CompactProtocolError("evidence ID numeric suffix is invalid") from exc
    if numeric_id > MAX_INTEGER:
        raise CompactProtocolError("evidence ID numeric suffix exceeds maximum")
    return _base36(numeric_id)


def _base36(value: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if value == 0:
        return "0"
    output = ""
    while value:
        value, remainder = divmod(value, 36)
        output = alphabet[remainder] + output
    return output


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CompactProtocolError(f"{name} is invalid")
    return value
