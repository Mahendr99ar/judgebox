"""Attacks a submission might try, and what the sandbox must do about each.

These run real processes in real namespaces, so they need root (CI runs
them with sudo). Each test checks two things: the verdict, and that nothing
leaked out of the box.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from judgebox.sandbox.runner import Limits, run

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="sandbox tests need root")

PY = "/usr/bin/python3"


@pytest.fixture
def box(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    os.chmod(d, 0o755)
    return d


def run_py(box: Path, code: str, limits: Limits = Limits(), stdin: str | None = None):
    (box / "main.py").write_text(textwrap.dedent(code))
    out = box.parent / "out.txt"
    inp = None
    if stdin is not None:
        inp = box.parent / "in.txt"
        inp.write_text(stdin)
    r = run([PY, "main.py"], box=box, limits=limits, stdin=inp, stdout=out)
    return r, out.read_text() if out.exists() else ""


def run_cpp(box: Path, code: str, limits: Limits = Limits()):
    (box / "main.cpp").write_text(textwrap.dedent(code))
    os.chmod(box, 0o777)
    c = run(["/usr/bin/g++", "-O2", "-o", "main", "main.cpp"], box=box, writable_box=True,
            limits=Limits(cpu_ms=20000, memory_kb=1024 * 1024), stdout=box.parent / "cc.txt")
    assert c.ok, c
    os.chmod(box, 0o755)
    out = box.parent / "out.txt"
    return run(["./main"], box=box, limits=limits, stdout=out), out


def test_normal_program(box):
    r, out = run_py(box, "print(sum(map(int, input().split())))", stdin="2 3\n")
    assert r.status == "OK" and out == "5\n"
    assert r.cpu_ms < 500


def test_infinite_loop_is_tle(box):
    r, _ = run_py(box, "while True: pass", Limits(cpu_ms=500))
    assert r.status == "TLE"
    assert r.wall_ms < 5000


def test_sleeping_is_tle_by_wall_clock(box):
    r, _ = run_py(box, "import time; time.sleep(60)", Limits(cpu_ms=500))
    assert r.status == "TLE"
    assert "wall clock" in r.detail
    assert r.wall_ms < 5000


def test_memory_bomb_is_mle(box):
    r, _ = run_cpp(box, """
        #include <vector>
        int main() {
            std::vector<char*> v;
            for (;;) {
                char* p = new char[1 << 20];
                for (int i = 0; i < (1 << 20); i += 4096) p[i] = 1;
                v.push_back(p);
            }
        }
    """, Limits(memory_kb=64 * 1024))
    assert r.status == "MLE"


def test_fork_bomb_is_contained(box):
    before = len(os.listdir("/proc"))
    r, _ = run_py(box, """
        import os
        while True:
            try:
                os.fork()
            except OSError:
                pass
    """, Limits(cpu_ms=1000, pids=32))
    assert r.status in ("TLE", "RE")
    time.sleep(0.2)
    # Every forked process died with pid 1 of the namespace.
    assert len(os.listdir("/proc")) <= before + 2


def test_output_flood_is_ole(box):
    r, _ = run_py(box, """
        import sys
        while True:
            sys.stdout.write("x" * 65536)
    """, Limits(output_kb=1024))
    assert r.status == "OLE"


def test_environment_is_empty(box, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "do-not-leak")
    r, out = run_py(box, "import os; print(sorted(os.environ))")
    assert r.ok
    assert "JWT_SECRET" not in out
    assert out.strip() == "['HOME', 'LANG', 'PATH']"


def test_host_files_are_not_visible(box, tmp_path):
    secret = Path("/tmp/judgebox-host-secret.txt")
    secret.write_text("database password")
    try:
        r, out = run_py(box, """
            import os
            for path in ["/tmp/judgebox-host-secret.txt", "/etc/shadow", "/etc/hostname", "/root"]:
                print(path, os.path.exists(path))
            print(" ".join(sorted(os.listdir("/"))))
        """)
    finally:
        secret.unlink()
    assert r.ok
    lines = out.splitlines()
    assert all(line.endswith("False") for line in lines[:4])
    visible = set(lines[4].split())
    assert {"box", "tmp", "usr", "proc"} <= visible
    assert not visible & {"home", "root", "var", "opt", "srv", "mnt", "run"}


def test_filesystem_is_read_only_except_tmp(box):
    r, out = run_py(box, """
        import errno

        def attempt(path):
            try:
                open(path, "w").write("x")
                return "written"
            except OSError as e:
                return errno.errorcode[e.errno]
        print(attempt("/usr/bin/pwned"), attempt("/box/pwned"), attempt("/pwned"), attempt("/tmp/scratch"))
    """)
    assert r.ok
    assert out.split() == ["EROFS", "EROFS", "EROFS", "written"]
    assert not (box / "pwned").exists()


def test_no_network(box):
    r, out = run_py(box, """
        import socket
        for host in ["1.1.1.1", "127.0.0.1"]:
            s = socket.socket()
            s.settimeout(2)
            try:
                s.connect((host, 80))
                print(host, "connected")
            except OSError as e:
                print(host, "blocked")
    """)
    assert r.ok
    assert "connected" not in out


def test_cannot_see_or_kill_host_processes(box):
    r, out = run_py(box, """
        import os, signal
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
        print(len(pids))
        try:
            os.kill(-1, signal.SIGKILL)  # everything we are allowed to signal
        except OSError:
            pass
    """)
    assert int(out.split()[0]) <= 2  # itself, maybe one short-lived helper
    assert subprocess.run(["true"]).returncode == 0  # we (the host) are still alive


def test_cannot_become_root(box):
    r, out = run_py(box, """
        import os
        print(os.getuid())
        try:
            os.setuid(0)
            print("root")
        except PermissionError:
            print("denied")
    """)
    assert out.split() == ["65534", "denied"]


def test_segfault_is_runtime_error(box):
    r, _ = run_cpp(box, "int main() { volatile int* p = nullptr; *p = 1; }")
    assert r.status == "RE" and r.detail == "SIGSEGV"


def test_compiler_cannot_read_device_forever(box):
    """`#include "/dev/urandom"` makes a naive judge hang in the compiler."""
    (box / "main.cpp").write_text('#include "/dev/urandom"\n')
    os.chmod(box, 0o777)
    r = run(["/usr/bin/g++", "-o", "main", "main.cpp"], box=box, writable_box=True,
            limits=Limits(cpu_ms=2000, memory_kb=512 * 1024), stdout=box.parent / "cc.txt")
    assert r.status in ("TLE", "MLE", "RE", "OLE")
    assert r.wall_ms < 15000


def test_deep_recursion_is_allowed(box):
    r, _ = run_cpp(box, """
        int f(int n) { volatile char pad[64]; pad[0] = n; return n ? f(n - 1) + pad[0] : 0; }
        int main() { return f(1000000) == 0; }
    """, Limits(memory_kb=512 * 1024))
    assert r.status in ("OK", "RE")  # RE only from the return code trick
    assert r.status != "MLE" and r.signal is None


def test_tle_is_caught_close_to_the_limit(box):
    """The CPU watchdog stops the run within a poll interval of the limit,
    not at the next whole second of RLIMIT_CPU."""
    r, _ = run_cpp(box, "int main() { volatile unsigned long x = 0; for (;;) x++; }", Limits(cpu_ms=1000))
    assert r.status == "TLE"
    assert 1000 <= r.cpu_ms < 1300


def test_program_is_not_pid_1_and_has_default_signals(box):
    # cat, not Python: CPython ignores SIGPIPE and SIGXFSZ itself at startup.
    out = box.parent / "status.txt"
    r = run(["/usr/bin/cat", "/proc/self/status"], box=box, limits=Limits(), stdout=out)
    status = dict(line.split(":\t", 1) for line in out.read_text().splitlines() if ":\t" in line)
    assert r.ok
    assert status["Pid"] == "2"
    assert int(status["SigIgn"], 16) == 0
