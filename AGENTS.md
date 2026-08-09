# Agent Loop System

## Setup and gates
- Install dev tools with `python3 -m pip install -e ".[dev]"`; there is no lockfile or alternate package manager here.
- CI runs on Python 3.11-3.14 and uses this gate order: `python -m pytest -q`, `ruff check .`, `python -m compileall -q src examples`, `python -m build`.
- Pytest is configured in `pyproject.toml` with `pythonpath = ["src"]`, so focused tests work as `python3 -m pytest tests/test_cli.py -q` or `python3 -m pytest tests/test_security_controls.py::test_allowed_commands_blocks_unapproved_worker_before_execution -q`.

## Running the surface
- Installed CLI entrypoint: `agent-loop examples/quality_gate_loop.json`; without install use `PYTHONPATH=src python3 -m agent_loop.cli examples/quality_gate_loop.json`.
- `examples/quality_gate_loop.json` only runs `python3 -m pytest -q` through its worker context; it is not the full CI gate.
- `examples/container_loop.json` needs Docker/Podman. The other public loop specs run locally with Python only.

## Architecture map
- `src/agent_loop/cli.py` loads a JSON spec, calls `run_command_loop`, prints a dataclass report as JSON, and returns `0` on success, `1` on evaluator failure, `2` on invalid specs/runtime errors.
- `src/agent_loop/command_runner.py` owns subprocess execution, spec validation, cwd/env/container handling, redaction, timeout/output caps, and adapts commands into `LoopEngine` callbacks.
- `src/agent_loop/engine.py` is the pure loop: it builds state with `goal`, `attempt`, `max_iterations`, `context`, `previous_feedback`, and `history`, then stops on evaluator pass or `max_iterations`.
- `src/agent_loop/__init__.py` exports only the engine dataclasses/classes; import `run_command_loop` from `agent_loop.command_runner`.

## Runtime contracts to preserve
- `work_command` and `eval_command` must be non-empty lists of strings; never change this to shell strings or `shell=True`.
- Workers read state JSON from stdin and should return `{"output": ..., "metadata": {...}}`; plain text worker stdout is accepted, but metadata must be an object when present.
- Evaluators read `{"state": ..., "result": ...}` from stdin and must return a JSON object with a real boolean `passed`; string booleans like `"false"` are intentionally rejected.
- `allowed_commands` checks the raw worker/evaluator executable before container wrapping and accepts either the exact executable or its basename.
- `command_env` is merged into `os.environ`, not isolated. Env values whose keys contain `secret`, `token`, `password`, `passwd`, `api_key`, `apikey`, or `key` are auto-redacted in reports.
- `max_output_chars` keeps the tail of worker output and command failure text; tests assert tail preservation.
- `command_timeout_seconds` appears only inside `examples/quality_gate_loop.json` `context`; the top-level harness timeout field is `timeout_seconds`.

## Tests and examples
- Tests create temporary worker/evaluator scripts and run real subprocesses; keep contract tests in `tests/test_production_contracts.py`, runtime tests in `tests/test_runtime_controls.py`, and security/redaction/container policy tests in `tests/test_security_controls.py` aligned with behavior changes.
- The container unit test fakes the container runtime with a temporary script, so it does not require Docker; only the public `examples/container_loop.json` does.
- `tests/test_security_controls.py::test_examples_include_multiple_public_loop_specs` expects the public `*_loop.json` specs for quality gate, allowlist, redaction, and container examples to remain present.
