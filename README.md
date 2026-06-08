<a id="readme-top"></a>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![Python][python-shield]][python-url]

<br />
<div align="center">
  <h1 align="center">Agent Loop System</h1>

  <p align="center">
    A production-oriented closed-loop agent harness for running real workers behind strict evaluator gates.
    <br />
    <a href="https://github.com/tylerdotai/agent-loop-system"><strong>Explore the source »</strong></a>
    <br />
    <br />
    <a href="https://github.com/tylerdotai/agent-loop-system/issues/new?labels=bug">Report Bug</a>
    &middot;
    <a href="https://github.com/tylerdotai/agent-loop-system/issues/new?labels=enhancement">Request Feature</a>
  </p>
</div>

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#built-with">Built With</a></li>
        <li><a href="#why-this-exists">Why This Exists</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#runtime-contract">Runtime Contract</a></li>
    <li><a href="#security-model">Security Model</a></li>
    <li><a href="#quality-gate">Quality Gate</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
  </ol>
</details>

## About The Project

Agent Loop System turns “looping” into a bounded control system:

```text
goal → worker → evaluator → feedback/history → retry → stop
```

It is intentionally small. The harness does not pretend to be the agent. It runs real commands, passes structured state over JSON, records history, and stops when the evaluator says the work passes or the iteration limit is reached.

Use it when you want a reliable local loop around agents, scripts, quality gates, research workflows, code generators, or any worker that can speak JSON over stdin/stdout.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

* [![Python][Python]][Python-url]
* `subprocess.run(..., shell=False)`
* JSON stdin/stdout contracts
* pytest test coverage

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Why This Exists

Most “agent loops” get hand-wavy fast. They retry blindly, accept fuzzy success, or hide critical behavior behind mocked internals.

This project keeps the boundary explicit:

* workers do real work
* evaluators make hard pass/fail decisions
* state and feedback move forward between attempts
* command specs are validated before execution
* evaluator gates require real JSON booleans
* subprocess output is capped
* loops are bounded by stop conditions

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Getting Started

### Prerequisites

* Python 3.11+
* `pip`
* Optional for development: `ruff`, `pytest`

### Installation

Clone the repository:

```sh
git clone https://github.com/tylerdotai/agent-loop-system.git
cd agent-loop-system
```

Install editable:

```sh
python3 -m pip install -e .
```

Run the CLI:

```sh
agent-loop examples/quality_gate_loop.json
```

If you do not want to install it yet, run directly from source:

```sh
PYTHONPATH=src python3 -m agent_loop.cli examples/quality_gate_loop.json
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Usage

Create a loop spec:

```json
{
  "goal": "Run the repository quality gate until it passes",
  "max_iterations": 1,
  "timeout_seconds": 180,
  "work_command": ["python3", "examples/quality_gate_worker.py"],
  "eval_command": ["python3", "examples/quality_gate_evaluator.py"],
  "context": {
    "command": ["python3", "-m", "pytest", "-q"],
    "cwd": ".",
    "command_timeout_seconds": 120
  }
}
```

Run it:

```sh
agent-loop examples/quality_gate_loop.json
```

Successful output has this shape:

```json
{
  "goal": "Run the repository quality gate until it passes",
  "success": true,
  "iterations": 1,
  "stop_reason": "eval_passed",
  "history": [
    {
      "attempt": 1,
      "output": "...",
      "passed": true,
      "eval_message": "quality gate passed",
      "metadata": {
        "returncode": 0,
        "command": ["python3", "-m", "pytest", "-q"],
        "cwd": "."
      }
    }
  ]
}
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Runtime Contract

A loop spec is a trusted local execution config.

Required fields:

* `goal`: non-empty string
* `work_command`: non-empty JSON array of strings
* `eval_command`: non-empty JSON array of strings

Optional fields:

* `max_iterations`: integer, default `5`
* `timeout_seconds`: positive integer, default `120`
* `context`: JSON object passed to worker/evaluator state
* `command_cwd`: existing directory used as the subprocess working directory
* `command_env`: JSON object of string environment variables merged into the subprocess environment
* `max_output_chars`: positive integer, default `12000`; worker output and command failure messages are capped to the tail of this size

### Worker Contract

Worker receives JSON state on `stdin`:

```json
{
  "goal": "...",
  "attempt": 1,
  "max_iterations": 5,
  "context": {},
  "previous_feedback": null,
  "history": []
}
```

Worker returns JSON on `stdout`:

```json
{
  "output": "work result",
  "metadata": {"returncode": 0}
}
```

Plain text output is accepted and treated as the worker `output`, but JSON is the production path.

### Evaluator Contract

Evaluator receives JSON on `stdin`:

```json
{
  "state": {"goal": "...", "attempt": 1},
  "result": {"output": "work result", "metadata": {}}
}
```

Evaluator must return JSON with a real boolean `passed`:

```json
{
  "passed": false,
  "message": "missing required gate"
}
```

Strings like `"false"`, `"no"`, or `"0"` are rejected. Eval gates need hard booleans, not vibes.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Security Model

This harness runs local subprocesses. It uses `shell=False`, but that does not sandbox the command.

Do not run untrusted specs.

A spec can point at commands that read files, write files, access the network, or use the current process environment. Treat specs the same way you would treat local shell scripts.

Current protections:

* no shell interpolation by the harness
* command arrays are validated before execution
* cwd/env controls are explicit
* evaluator `passed` must be a JSON boolean
* malformed specs fail with a clean CLI error
* subprocess output is capped
* timeouts are enforced

Not included yet:

* OS-level sandboxing
* container isolation
* policy enforcement for allowed commands
* secret redaction from worker output

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Quality Gate

Run the full local gate:

```sh
python3 -m pytest -q
ruff check .
python3 -m compileall -q src examples
```

At publication time, the project passed:

```text
13 passed
All checks passed!
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Roadmap

- [x] Strict worker/evaluator subprocess contract
- [x] Strict evaluator boolean gate
- [x] Context passthrough
- [x] cwd/env runtime controls
- [x] Output caps and timeout handling
- [x] Installable CLI
- [ ] Command allowlist policy
- [ ] Optional containerized execution
- [ ] Secret redaction filters
- [x] GitHub Actions CI
- [ ] More worker/evaluator examples

See the [open issues](https://github.com/tylerdotai/agent-loop-system/issues) for proposed features and known issues.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Contributing

Contributions are welcome.

1. Fork the project
2. Create your feature branch (`git checkout -b feature/my-feature`)
3. Run the quality gate
4. Commit your changes (`git commit -m 'Add my feature'`)
5. Push to your branch (`git push origin feature/my-feature`)
6. Open a pull request

Please keep the core small and the runtime boundary explicit. If a change weakens spec validation, evaluator strictness, or stop-condition behavior, it needs a very good reason.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## License

Distributed under the MIT License. See `LICENSE` for more information.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Contact

Tyler Delano - [@tylerdotai](https://github.com/tylerdotai)

Project Link: [https://github.com/tylerdotai/agent-loop-system](https://github.com/tylerdotai/agent-loop-system)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

[contributors-shield]: https://img.shields.io/github/contributors/tylerdotai/agent-loop-system.svg?style=for-the-badge
[contributors-url]: https://github.com/tylerdotai/agent-loop-system/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/tylerdotai/agent-loop-system.svg?style=for-the-badge
[forks-url]: https://github.com/tylerdotai/agent-loop-system/network/members
[stars-shield]: https://img.shields.io/github/stars/tylerdotai/agent-loop-system.svg?style=for-the-badge
[stars-url]: https://github.com/tylerdotai/agent-loop-system/stargazers
[issues-shield]: https://img.shields.io/github/issues/tylerdotai/agent-loop-system.svg?style=for-the-badge
[issues-url]: https://github.com/tylerdotai/agent-loop-system/issues
[license-shield]: https://img.shields.io/github/license/tylerdotai/agent-loop-system.svg?style=for-the-badge
[license-url]: https://github.com/tylerdotai/agent-loop-system/blob/main/LICENSE
[python-shield]: https://img.shields.io/badge/python-3.11%2B-blue.svg?style=for-the-badge&logo=python&logoColor=white
[python-url]: https://www.python.org/
[Python]: https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white
[Python-url]: https://www.python.org/
