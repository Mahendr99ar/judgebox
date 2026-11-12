"""Per-run control groups.

Rlimits alone are not enough for a judge: RLIMIT_AS counts virtual memory
(the JVM reserves gigabytes it never touches) and RLIMIT_NPROC is per user
across the whole machine, so two sandboxes running as `nobody` would share
one fork budget. A cgroup gives each run its own memory, pid and CPU
accounting.

Both hierarchies exist in the wild. Ubuntu 22.04+ and most cloud VMs use
cgroup v2; older kernels and some container hosts still mount v1. The two
classes below expose the same small interface.
"""
from __future__ import annotations

import errno
import os
import signal
import time
import uuid
from pathlib import Path

ROOT = Path("/sys/fs/cgroup")
PARENT = "judgebox"


def _write(path: Path, value: str | int) -> None:
    path.write_text(str(value))


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text().split()[0])
    except (FileNotFoundError, ValueError, IndexError):
        return 0


def _read_kv(path: Path) -> dict[str, int]:
    out = {}
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition(" ")
            if v.strip().lstrip("-").isdigit():
                out[k] = int(v)
    except FileNotFoundError:
        pass
    return out


def _rmdir_when_empty(path: Path, tries: int = 50) -> None:
    for _ in range(tries):
        try:
            path.rmdir()
            return
        except FileNotFoundError:
            return
        except OSError:
            # still has tasks for a few ms after SIGKILL
            time.sleep(0.01)


CONTROLLERS = "+memory +pids +cpu"


def _enable_v2_controllers() -> None:
    """Let child cgroups use memory, pids and cpu.

    On a host, /sys/fs/cgroup is the real root and this just works. Inside a
    container it is the container's own cgroup, which already holds our
    processes, and v2 refuses to enable controllers for a cgroup that has
    processes of its own (EBUSY). The usual fix, the same one Docker-in-Docker
    uses, is to move those processes into a child group first.
    """
    try:
        _write(ROOT / "cgroup.subtree_control", CONTROLLERS)
    except OSError as exc:
        if exc.errno != errno.EBUSY:
            raise
        supervisor = ROOT / "supervisor"
        supervisor.mkdir(exist_ok=True)
        for pid in _read_pids(ROOT / "cgroup.procs"):
            try:
                _write(supervisor / "cgroup.procs", pid)
            except OSError:
                pass  # exited meanwhile
        _write(ROOT / "cgroup.subtree_control", CONTROLLERS)


class CgroupV2:
    def __init__(self, memory_bytes: int, pids: int):
        parent = ROOT / PARENT
        if not parent.exists():
            _enable_v2_controllers()
            parent.mkdir()
        _write(parent / "cgroup.subtree_control", CONTROLLERS)
        self.path = parent / f"run-{uuid.uuid4().hex[:12]}"
        self.path.mkdir()
        _write(self.path / "memory.max", memory_bytes)
        _write(self.path / "memory.swap.max", 0)
        _write(self.path / "pids.max", pids)

    def add(self, pid: int) -> None:
        _write(self.path / "cgroup.procs", pid)

    def cpu_ns(self) -> int:
        return _read_kv(self.path / "cpu.stat").get("usage_usec", 0) * 1000

    def peak_bytes(self) -> int:
        # memory.peak needs Linux 5.19; older kernels only have the current value.
        return _read_int(self.path / "memory.peak") or _read_int(self.path / "memory.current")

    def oom_kills(self) -> int:
        return _read_kv(self.path / "memory.events").get("oom_kill", 0)

    def kill_all(self) -> None:
        kill_file = self.path / "cgroup.kill"
        if kill_file.exists():
            _write(kill_file, 1)
            return
        for pid in _read_pids(self.path / "cgroup.procs"):
            _kill(pid)

    def destroy(self) -> None:
        self.kill_all()
        _rmdir_when_empty(self.path)


class CgroupV1:
    def __init__(self, memory_bytes: int, pids: int):
        name = f"{PARENT}-{uuid.uuid4().hex[:12]}"
        self.mem = ROOT / "memory" / name
        self.pids = ROOT / "pids" / name
        self.cpu = ROOT / "cpuacct" / name
        for d in (self.mem, self.pids, self.cpu):
            d.mkdir()
        _write(self.mem / "memory.limit_in_bytes", memory_bytes)
        swap = self.mem / "memory.memsw.limit_in_bytes"
        if swap.exists():
            _write(swap, memory_bytes)
        _write(self.pids / "pids.max", pids)

    def add(self, pid: int) -> None:
        for d in (self.mem, self.pids, self.cpu):
            _write(d / "cgroup.procs", pid)

    def cpu_ns(self) -> int:
        return _read_int(self.cpu / "cpuacct.usage")

    def peak_bytes(self) -> int:
        return _read_int(self.mem / "memory.max_usage_in_bytes")

    def oom_kills(self) -> int:
        kv = _read_kv(self.mem / "memory.oom_control")
        if "oom_kill" in kv:
            return kv["oom_kill"]
        # Kernels before 4.13 have no counter; a hit on the limit is the best signal.
        return 1 if _read_int(self.mem / "memory.failcnt") else 0

    def kill_all(self) -> None:
        for pid in _read_pids(self.pids / "cgroup.procs"):
            _kill(pid)

    def destroy(self) -> None:
        self.kill_all()
        for d in (self.mem, self.pids, self.cpu):
            _rmdir_when_empty(d)


def _read_pids(path: Path) -> list[int]:
    try:
        return [int(x) for x in path.read_text().split()]
    except FileNotFoundError:
        return []


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def detect() -> type[CgroupV2] | type[CgroupV1] | None:
    """Pick the hierarchy that actually has the controllers we need."""
    controllers = ROOT / "cgroup.controllers"
    if controllers.exists() and "memory" in controllers.read_text().split():
        return CgroupV2
    if (ROOT / "memory" / "memory.limit_in_bytes").exists() and (ROOT / "pids").is_dir():
        return CgroupV1
    return None
