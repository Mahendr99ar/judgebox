"""Run one command in a fresh sandbox and classify how it ended."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from judgebox.sandbox import cgroups

DEFAULT_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/box",
    "LANG": "C.UTF-8",
}


@dataclass(frozen=True)
class Limits:
    cpu_ms: int = 1000
    wall_ms: int | None = None  # defaults to 3x cpu + 1 s, so sleeping cannot dodge TLE
    memory_kb: int = 256 * 1024
    pids: int = 64
    output_kb: int = 64 * 1024
    tmpfs_kb: int = 16 * 1024
    stack_unlimited: bool = True

    @property
    def wall(self) -> int:
        return self.wall_ms or self.cpu_ms * 3 + 1000


@dataclass
class RunResult:
    status: str  # OK, TLE, MLE, OLE, RE, SANDBOX_ERROR
    exit_code: int | None
    signal: int | None
    cpu_ms: int
    wall_ms: int
    memory_kb: int
    stderr: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK"


class SandboxUnavailable(RuntimeError):
    pass


def check_host() -> None:
    """Fail early with a clear message instead of on the first submission."""
    if os.geteuid() != 0:
        raise SandboxUnavailable("the sandbox needs root (or a privileged container) to create namespaces")
    if not hasattr(os, "unshare"):
        raise SandboxUnavailable("Python 3.12+ is required for os.unshare")


def run(
    argv: list[str],
    *,
    box: str | Path,
    limits: Limits,
    stdin: str | Path | None = None,
    stdout: str | Path,
    stderr: str | Path | None = None,
    writable_box: bool = False,
    env: dict[str, str] | None = None,
) -> RunResult:
    cg_class = cgroups.detect()
    cg = cg_class(limits.memory_kb * 1024, limits.pids) if cg_class else None
    root = tempfile.mkdtemp(prefix="jb-root-")
    stderr_path = Path(stderr) if stderr else Path(root + ".stderr")

    cfg = {
        "argv": argv,
        "env": env or DEFAULT_ENV,
        "box": str(Path(box).resolve()),
        "writable_box": writable_box,
        "root": root,
        "stdin": str(stdin) if stdin else None,
        "stdout": str(stdout),
        "stderr": str(stderr_path),
        "cpu_ms": limits.cpu_ms,
        "wall_ms": limits.wall,
        "memory_kb": limits.memory_kb,
        "output_kb": limits.output_kb,
        "tmpfs_kb": limits.tmpfs_kb,
        "stack_unlimited": limits.stack_unlimited,
        "cgroup_procs": _procs_files(cg),
        "cgroup_cpu_file": _cpu_file(cg),
        # Without cgroups, fall back to rlimits: address space for memory and a
        # per-user process cap for fork bombs. Both are cruder; see docs.
        "address_space_kb": 0 if cg else limits.memory_kb * 2,
        "nproc": 0 if cg else limits.pids,
    }

    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "judgebox.sandbox.init", json.dumps(cfg)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    try:
        out, err = proc.communicate(input=b"g", timeout=limits.wall / 1000 + 5)
    except subprocess.TimeoutExpired:
        # init's own timer should have fired long before this. Clean up hard.
        if cg:
            cg.kill_all()
        proc.send_signal(signal.SIGKILL)
        out, err = proc.communicate()
    wall_ms = int((time.monotonic() - started) * 1000)

    try:
        report = json.loads(out.decode().strip().splitlines()[-1])
    except (IndexError, ValueError):
        report = {"setup_error": f"init failed: {err.decode(errors='replace')[-2000:]}"}

    cpu_ms = report.get("cpu_ms", 0)
    memory_kb = report.get("max_rss_kb", 0)
    oom = False
    if cg:
        # rusage also counts the sandbox setup before exec; the cgroup does not.
        cpu_ms = cg.cpu_ns() // 1_000_000
        memory_kb = cg.peak_bytes() // 1024 or memory_kb
        oom = cg.oom_kills() > 0
        cg.destroy()

    stderr_text = _tail(stderr_path)
    if not stderr:
        stderr_path.unlink(missing_ok=True)
    shutil.rmtree(root, ignore_errors=True)

    result = RunResult(
        status="OK",
        exit_code=report.get("exit_code"),
        signal=report.get("signal"),
        cpu_ms=cpu_ms,
        wall_ms=wall_ms,
        memory_kb=memory_kb,
        stderr=stderr_text,
    )
    try:
        output_bytes = os.path.getsize(stdout)
    except OSError:
        output_bytes = 0
    result.status, result.detail = _classify(report, result, limits, oom, output_bytes)
    return result


def _classify(report: dict, r: RunResult, limits: Limits, oom: bool, output_bytes: int) -> tuple[str, str]:
    if report.get("setup_error"):
        return "SANDBOX_ERROR", report["setup_error"]
    sig = r.signal
    # Order matters: an OOM kill and a CPU overrun both arrive as SIGKILL.
    if oom or (sig == signal.SIGKILL and r.memory_kb >= limits.memory_kb * 0.95):
        return "MLE", f"peak {r.memory_kb} KB"
    # C and C++ die from SIGXFSZ; CPython ignores that signal and fails with
    # EFBIG instead, so the file size is the check that works for every language.
    if sig == signal.SIGXFSZ or output_bytes >= limits.output_kb * 1024:
        return "OLE", f"output over {limits.output_kb} KB"
    if report.get("cpu_timeout") or r.cpu_ms > limits.cpu_ms or sig == signal.SIGXCPU:
        return "TLE", f"cpu {r.cpu_ms} ms"
    if report.get("wall_timeout"):
        return "TLE", f"wall clock over {limits.wall} ms (cpu {r.cpu_ms} ms)"
    if sig is not None:
        return "RE", signal.Signals(sig).name
    if r.exit_code:
        return "RE", f"exit code {r.exit_code}"
    return "OK", ""


def _procs_files(cg) -> list[str]:
    if cg is None:
        return []
    if isinstance(cg, cgroups.CgroupV2):
        return [str(cg.path / "cgroup.procs")]
    return [str(d / "cgroup.procs") for d in (cg.mem, cg.pids, cg.cpu)]


def _cpu_file(cg) -> str | None:
    if cg is None:
        return None
    if isinstance(cg, cgroups.CgroupV2):
        return str(cg.path / "cpu.stat")
    return str(cg.cpu / "cpuacct.usage")


def _tail(path: Path, limit: int = 4096) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode(errors="replace")
    except FileNotFoundError:
        return ""
