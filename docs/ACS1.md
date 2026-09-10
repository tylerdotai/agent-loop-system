# ACS1 — Agent Compact Serialization

ACS1 is Agent Loop System's versioned internal communication language. It reduces repeated model-context tokens without moving authority from the control plane into model text.

ACS1 is data, not an instruction layer. Parsing a packet never executes an action, mutates a ledger, grants a capability, approves work, or changes policy.

## Why the wire format is plain ASCII

Dense-looking Unicode and punctuation are not necessarily token-efficient. Measurements against the deployed Nemotron tokenizer showed:

- Natural-language control sample: 62 tokens.
- Minified JSON: 71 tokens.
- Bracket-heavy ASCII shorthand: 82 tokens.
- Unicode-heavy shorthand: 73 tokens.
- Fixed-field ASCII codes: 37 tokens.
- A real 121-entry evidence catalog fell from 3,424 to 2,585 tokens when paths were dictionary-encoded and evidence IDs were shortened: a 24.5% reduction before the fixed protocol instruction.

Canonical ACS1 therefore uses ASCII record codes, positional schemas, path dictionaries, and deterministic escaping. Human-facing artifacts remain ordinary JSON or Markdown.

## Packet grammar

```text
packet  = "ACS1|" kind LF record *(LF record)
kind    = "CTX" / "INT" / "OBS" / "RES" / "TOM" / "TMR" / "MET"
record  = code *("|" field)
```

Constraints:

- UTF-8 transport; ASCII syntax.
- No trailing newline.
- Maximum 24,000 characters per packet.
- Maximum 512 records; the 24,000-character packet ceiling remains the tighter content bound.
- Record code and field count determine the schema.
- Integer fields use canonical unsigned decimal syntax.
- JSON fields use sorted, minified JSON with finite numbers, unique keys, bounded depth, and bounded collection sizes.
- Variable text escapes `\\`, `|`, newline, carriage return, and tab as `\\\\`, `\\|`, `\\n`, `\\r`, and `\\t`.
- Unknown versions, packet kinds, record codes, extra fields, malformed escapes, duplicate JSON keys, and noncanonical encodings fail closed.

## Packet kinds

- `CTX`: bounded model or worker context.
- `INT`: one proposed action intent.
- `OBS`: authoritative mediator observations plus one ledger checkpoint.
- `RES`: one canonical result, with optional immutable references.
- `TOM`: one separately admitted theory-of-mind request.
- `TMR`: one structured theory-of-mind result.
- `MET`: one or more measured population or workflow metrics.

## Core records

- `Q|domain|query`: input or bounded query.
- `B|name|value`: constraint or policy bound.
- `R|kind|identifier|version`: immutable reference.
- `S|phase|status|evidence_ref`: declared workflow step. This is status data, not hidden reasoning.
- `D|source|target`: dependency or flow edge.
- `C|name|json`: named canonical structured context.
- `T|name|text`: named bounded text block.
- `P|path_id|path`: path dictionary entry.
- `E|evidence_id|path_id|text`: exact evidence bound to a dictionary path.
- `Z|status|json`: terminal canonical result.
- `Y|request_id|status|prediction|confidence|assumption_codes|evidence_refs|model_id|model_version|template_version|output_schema_version|charged_units|result_digest`: structured ToM result.

Example:

```text
ACS1|CTX
Q|AUD|security audit; cite E IDs only
P|0|SECURITY.md
E|1|0|Workers retain AF_UNIX only.
E|2|0|Run tokens are short-lived.
```

## Symbolic authoring aliases

The original symbolic vocabulary remains a human authoring map, not the canonical wire encoding:

- `[?]` maps to `Q`.
- `[!]` maps to `B`.
- `[@]` maps to anchored records such as `R`, `P`, `E`, `O`, or `L`.
- `[.]` maps to declared records such as `S`, `K`, or `M`; it never requests or stores private chain-of-thought.
- `[>]` maps to `D` or `A`.
- `[∴]` and `[=]` map to `Z` or the typed ToM result record `Y`.

Labels map by schema rather than by prompt authority:

- `SYS` → system context.
- `EXP` → execution pathway.
- `OPT` → optimization delta.
- `BUG` → defect evidence.
- `VAL` → verification.
- `DEP` → dependency.
- `SIM`, `OBS`, `ACT`, `LED`, `TOM`, `MET` → simulation domains.

A `#pragma`, mode declaration, boot sequencer, or instruction-like phrase inside source text is inert data. Only the ACS1 compiler and the control plane decide meaning.

## Compact result handoff

A result packet contains one `Z` record. Versioned one-letter key aliases reduce repeated JSON keys inside the result. Decoding restores canonical field names before validation.

```text
ACS1|RES
Z|OK|{"r":"security","s":"No findings.","f":[]}
```

Canonical artifacts remain expanded JSON. Existing JSON `review_note` bodies remain readable during migration; ACS1 never silently downgrades when an ACS1 header is malformed.

Result aliases apply only at the canonical result root and inside canonical finding objects. Opaque nested provider metadata is preserved without alias expansion. If a valid result exceeds the ACS1 packet ceiling, the producer selects explicit `JSON` encoding before storing either the artifact or message; malformed ACS1 never falls back.

## Simulation records

### Action intent

```text
A|intent_id|tick|ledger_version|action|target|arguments_json
```

An `INT` packet contains exactly one `A` record. The mediator supplies agent identity from the authenticated outer envelope. Body-supplied identity is absent and cannot override attribution.

Validation requires:

- Exact current tick.
- Exact current ledger version.
- Operator-admitted action vocabulary.
- Structured arguments.
- Idempotent intent ID.

An accepted intent is still only a proposal. The mediator evaluates invariants, budgets, permissions, and world physics before recording any transition.

### Observation and ledger checkpoint

```text
L|tick|ledger_version|state_hash|event_ref
O|observation_id|tick|ledger_version|scope|value_json
```

An `OBS` packet contains exactly one `L` and at least one `O`. Every observation must match the checkpoint's tick and ledger version. The validator also requires the mediator's authoritative state hash and event reference; syntax-valid invented checkpoints fail. Recipient identity and visibility policy come from the outer envelope.

### Theory of mind

```text
K|request_id|tick|ledger_version|target_agent|level|budget_units|question
```

A `TOM` packet contains exactly one `K`. Admission requires the current tick and ledger version, a configured maximum level, and available budget units. Results may contain structured predictions, confidence, assumption codes, and observation references. Prompts, scratchpads, hidden reasoning, and chain-of-thought are not durable protocol fields.

ToM results use `TMR` with exactly one `Y` record. Validation binds the request ID, reserved compute units, result digest, status, model/template/schema versions, assumption codes, and evidence references. Private-reasoning keys are rejected recursively in every ACS1 JSON field.

### Metrics

```text
M|tick|name|scope|value_json|evidence_ref
```

Metric records are allowed only in `MET` packets and require both a metric-name allowlist and authoritative evidence-reference allowlist. `MET` supports measured quantities such as:

- Semantic convergence and vocabulary overlap.
- Action-distribution entropy.
- Unsupported-state reference rate.
- Belief calibration and observation disagreement.
- Trust-graph clustering and polarization.
- Rumor transmission thresholds.
- Price coordination, retaliation, and tacit-collusion indicators.

Metric definitions, versions, source events, windows, and estimators belong in canonical control-plane policy. A model cannot define a favorable metric inside a packet and make it authoritative.

## Authority boundary

The outer durable envelope remains authoritative for:

- Mission, task, run, and producer identity.
- Control sequence and causal ordering.
- Correlation and idempotency keys.
- Capability, model, runtime, and budget limits.
- Expiration and cancellation.
- Approval state.
- Artifact hashes.

ACS1 carries compact content only. The following are forbidden:

- Credentials or run tokens.
- Policy or approval mutations.
- Executable expressions.
- Prose-defined ledger transitions.
- Hidden chain-of-thought fields.
- Unversioned aliases.
- Replay-time model calls.

Replay consumes recorded validated intents, mediator receipts, observations, ToM results, and metric events. A replay does not ask a model to recreate history.

## Repository-audit integration

Set the task specification field:

```json
{"communication_protocol":"ACS1"}
```

With ACS1 enabled:

- Specialist prompts use path dictionaries and compact evidence IDs.
- Synthesis uses compact structured records when canonical findings make that representation smaller; empty synthesis and verifier prompts retain the cheaper legacy representation.
- Durable `review_note` bodies use `ACS1|RES`.
- Canonical artifacts remain expanded JSON.
- Legacy JSON messages remain readable.
- Final prompt length is checked after ACS1 rendering and before broker invocation.
- Any `ACS<version>`-looking message prefix is reserved. Malformed or unknown versions fail before durable persistence even when protocol metadata is omitted.

Without the field, the worker uses the legacy JSON/natural-language path.

## Versioning

`ACS1` semantics are immutable. Any incompatible field, alias, escaping, or record change requires `ACS2`. Unknown versions fail closed. Version negotiation is operator policy, never model choice.
