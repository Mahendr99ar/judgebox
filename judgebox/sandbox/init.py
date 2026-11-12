"""The process that builds the sandbox and runs one program inside it.

Started by runner.py as `python -m judgebox.sandbox.init '<json config>'`.

    init        root, new mount/net/ipc/uts namespaces; enforces the limits
     └── reaper   pid 1 of a new pid namespace; builds the root and chroots
          └── program   pid 2; uid 65534, rlimits, joins the cgroup, execs

Why the extra reaper: the kernel does not deliver default-action signals
(SIGXCPU, SIGTERM, ...) to pid 1 of a namespace, so a submission running as
pid 1 would shrug off its CPU rlimit. As pid 2 it behaves normally. When the
reaper exits, the kernel kills everything left in the namespace, so a fork
bomb cannot outlive the run.

init stays outside the cgroup so its own Python heap is not billed to the
submission. It watches the cgroup's CPU counter and kills the run as soon as
the limit is crossed, instead of waiting for the next whole second of
RLIMIT_CPU, which stays as a backstop for hosts without cgroups.
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import resource
import signal
import sys
import time

libc = ctypes.CDLL(None, use_errno=True)

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_BIND = 4096
MS_REC = 16384
MS_PRIVATE = 1 << 18

PR_SET_PDEATHSIG = 1
PR_SET_NO_NEW_PRIVS = 38

NOBODY = 65534
CPU_GRACE_MS = 50
POLL_S = 0.01

# Enough of /etc for the dynamic linker and the JDK, nothing else. The judge's
# own config and anything with secrets stays invisible.
ETC_ALLOWLIST = (
    "ld.so.cache",
    "ld.so.conf",
    "ld.so.conf.d",
    "alternatives",
    "java-*-openjdk",
    "localtime",
)


def _mount(src: str | None, target: str, fstype: str | None, flags: int, data: str | None = None) -> None:
    rc = libc.mount(
        src.encode() if src else None,
        target.encode(),
        fstype.encode() if fstype else None,
        flags,
        data.encode() if data else None,
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"mount {src} -> {target}: {os.strerror(err)}")


def _bind(src: str, target: str, readonly: bool = True) -> None:
    if os.path.isdir(src):
        os.makedirs(target, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "a").close()
    _mount(src, target, None, MS_BIND | MS_REC)
    if readonly:
        # A bind mount ignores MS_RDONLY on creation; it has to be remounted.
        _mount(None, target, None, MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID)


def _build_root(cfg: dict) -> None:
    root = cfg["root"]
    _mount(None, "/", None, MS_REC | MS_PRIVATE)  # keep our mounts out of the host
    _mount("tmpfs", root, "tmpfs", MS_NOSUID | MS_NODEV, "size=1m,mode=755")

    _bind("/usr", f"{root}/usr")
    for name in ("bin", "sbin", "lib", "lib32", "lib64"):
        host = f"/{name}"
        if os.path.islink(host):
            os.symlink(os.readlink(host), f"{root}/{name}")  # usr-merged distros
        elif os.path.isdir(host):
            _bind(host, f"{root}/{name}")

    os.makedirs(f"{root}/etc")
    for pattern in ETC_ALLOWLIST:
        for host in glob.glob(f"/etc/{pattern}"):
            _bind(host, f"{root}{host}")

    for dev in ("null", "zero", "urandom", "random"):
        _bind(f"/dev/{dev}", f"{root}/dev/{dev}", readonly=False)

    _bind(cfg["box"], f"{root}/box", readonly=not cfg["writable_box"])

    os.makedirs(f"{root}/tmp")
    _mount("tmpfs", f"{root}/tmp", "tmpfs", MS_NOSUID | MS_NODEV, f"size={cfg['tmpfs_kb']}k,mode=1777")

    os.makedirs(f"{root}/proc")
    _mount("proc", f"{root}/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)

    _mount(None, root, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV)
    os.chroot(root)
    os.chdir("/box")


def _set_limits(cfg: dict) -> None:
    cpu_s = cfg["cpu_ms"] // 1000 + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    out = cfg["output_kb"] * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (out, out))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    stack = resource.RLIM_INFINITY if cfg["stack_unlimited"] else cfg["memory_kb"] * 1024
    resource.setrlimit(resource.RLIMIT_STACK, (stack, stack))
    if cfg["address_space_kb"]:
        # Only used when there is no memory cgroup (see runner.py).
        a = cfg["address_space_kb"] * 1024
        resource.setrlimit(resource.RLIMIT_AS, (a, a))
    if cfg["nproc"]:
        resource.setrlimit(resource.RLIMIT_NPROC, (cfg["nproc"], cfg["nproc"]))


def _exec_program(cfg: dict, stdio: tuple[int, int, int], cgroup_fds: list[int], err_w: int) -> None:
    """pid 2: drop everything and become the submission."""
    try:
        _set_limits(cfg)
        for n, fd in enumerate(stdio):
            os.dup2(fd, n)

        # CPython ignores SIGPIPE and SIGXFSZ, and ignored signals survive exec.
        # The submission should get the default behaviour.
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            signal.signal(sig, signal.SIG_DFL)

        keep = sorted({err_w, *cgroup_fds})
        start = 3
        for fd in keep:
            os.closerange(start, fd)
            start = fd + 1
        os.closerange(start, 1 << 16)

        os.setgroups([])
        os.setgid(NOBODY)
        os.setuid(NOBODY)
        libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        # Cleared by setuid, so it has to come after it.
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)

        # Join the cgroup last: everything before exec is our overhead.
        for fd in cgroup_fds:
            os.write(fd, b"0")  # "0" means the writing process
            os.close(fd)
        os.execve(cfg["argv"][0], cfg["argv"], cfg["env"])
    except BaseException as exc:  # noqa: BLE001 - anything here is a sandbox failure
        os.write(err_w, f"{type(exc).__name__}: {exc}".encode()[:2000])
    finally:
        os._exit(127)


def _reaper(cfg: dict, err_w: int, report_w: int) -> None:
    """pid 1 of the namespace."""
    try:
        cgroup_fds = [os.open(p, os.O_WRONLY) for p in cfg["cgroup_procs"]]
        stdio = (
            os.open(cfg["stdin"] or "/dev/null", os.O_RDONLY),
            os.open(cfg["stdout"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644),
            os.open(cfg["stderr"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644),
        )
        _build_root(cfg)
    except BaseException as exc:  # noqa: BLE001
        os.write(err_w, f"{type(exc).__name__}: {exc}".encode()[:2000])
        os._exit(127)

    program = os.fork()
    if program == 0:
        os.close(report_w)
        _exec_program(cfg, stdio, cgroup_fds, err_w)

    for fd in (*stdio, *cgroup_fds, err_w):
        os.close(fd)
    # Reap anything that gets reparented to us until the program itself exits.
    while True:
        pid, status, usage = os.wait4(-1, 0)
        if pid == program:
            break
    os.write(report_w, json.dumps({
        "exit_code": os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
        "signal": os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
        "cpu_ms": int((usage.ru_utime + usage.ru_stime) * 1000),
        "max_rss_kb": usage.ru_maxrss,
    }).encode())
    os._exit(0)


def _cpu_reader(cfg: dict):
    path = cfg.get("cgroup_cpu_file")
    if not path:
        return None
    usec = path.endswith("cpu.stat")

    def read_ms() -> int:
        try:
            with open(path) as f:
                if usec:
                    for line in f:
                        if line.startswith("usage_usec"):
                            return int(line.split()[1]) // 1000
                    return 0
                return int(f.read()) // 1_000_000
        except (OSError, ValueError):
            return 0

    return read_ms


def _drain(fd: int) -> bytes:
    data = b""
    while chunk := os.read(fd, 4096):
        data += chunk
    return data


def main() -> None:
    cfg = json.loads(sys.argv[1])
    sys.stdin.buffer.read(1)  # wait until the runner has set up the cgroup

    os.unshare(os.CLONE_NEWNS | os.CLONE_NEWPID | os.CLONE_NEWNET | os.CLONE_NEWIPC | os.CLONE_NEWUTS)
    err_r, err_w = os.pipe2(os.O_CLOEXEC)
    report_r, report_w = os.pipe2(os.O_CLOEXEC)
    reaper = os.fork()
    if reaper == 0:
        os.close(err_r)
        os.close(report_r)
        _reaper(cfg, err_w, report_w)
    os.close(err_w)
    os.close(report_w)

    cpu_ms = _cpu_reader(cfg)
    deadline = time.monotonic() + cfg["wall_ms"] / 1000
    killed_for = None
    while True:
        pid, status = os.waitpid(reaper, os.WNOHANG)
        if pid:
            break
        if killed_for is None:
            if time.monotonic() > deadline:
                killed_for = "wall"
            elif cpu_ms and cpu_ms() > cfg["cpu_ms"] + CPU_GRACE_MS:
                killed_for = "cpu"
            if killed_for:
                os.kill(reaper, signal.SIGKILL)  # takes the whole namespace with it
        time.sleep(POLL_S)

    setup_error = _drain(err_r).decode(errors="replace")
    report = _drain(report_r)
    if report:
        result = json.loads(report)
    else:
        result = {"exit_code": None, "signal": signal.SIGKILL, "cpu_ms": 0, "max_rss_kb": 0}
        if not killed_for and not setup_error:
            setup_error = f"reaper exited without a report (status {status})"
    result.update({
        "wall_timeout": killed_for == "wall",
        "cpu_timeout": killed_for == "cpu",
        "setup_error": setup_error or None,
    })
    print(json.dumps(result))


if __name__ == "__main__":
    main()
