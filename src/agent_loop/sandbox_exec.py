from __future__ import annotations

import ctypes
import os
import signal
import sys
import time
from pathlib import Path
from typing import Sequence


_PR_SET_CHILD_SUBREAPER = 36


def _set_child_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _descendant_pids(root_pid: int) -> set[int]:
    known = {root_pid}
    descendants: set[int] = set()
    for _ in range(16):
        discovered: set[int] = set()
        try:
            entries = tuple(Path("/proc").iterdir())
        except OSError:
            return descendants
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                parent_pid = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
            pid = int(entry.name)
            if parent_pid in known and pid not in known:
                discovered.add(pid)
        if not discovered:
            break
        known.update(discovered)
        descendants.update(discovered)
    return descendants


def _kill_and_reap_descendants(root_pid: int) -> None:
    for pid in _descendant_pids(root_pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.01)


def _exit_code(wait_status: int) -> int:
    if os.WIFEXITED(wait_status):
        return os.WEXITSTATUS(wait_status)
    if os.WIFSIGNALED(wait_status):
        return 128 + os.WTERMSIG(wait_status)
    return 126


def _parse(arguments: Sequence[str]) -> tuple[Path | None, list[str]]:
    values = list(arguments)
    cgroup: Path | None = None
    if values[:1] == ["--cgroup"]:
        if len(values) < 4:
            raise ValueError("sandbox launcher requires a cgroup path and command")
        cgroup = Path(values[1])
        values = values[2:]
    if not values or values[0] != "--" or len(values) == 1:
        raise ValueError("sandbox launcher requires -- followed by a non-empty command")
    return cgroup, values[1:]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        cgroup, command = _parse(arguments)
        _set_child_subreaper()
        if cgroup is not None:
            (cgroup / "cgroup.procs").write_text(str(os.getpid()), encoding="ascii")
    except (OSError, ValueError) as exc:
        print(f"sandbox launcher failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 126

    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.setpgid(0, 0)
            os.execvpe(command[0], command, os.environ)
        except OSError as exc:
            print(f"sandbox worker exec failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            os._exit(126)

    try:
        while True:
            try:
                _, wait_status = os.waitpid(child_pid, 0)
                break
            except InterruptedError:
                continue
    finally:
        _kill_and_reap_descendants(os.getpid())
    return _exit_code(wait_status)


if __name__ == "__main__":
    raise SystemExit(main())
