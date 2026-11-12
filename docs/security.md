# What the sandbox is tested against

Every item below is a test in `tests/test_sandbox.py` that runs real code in
the sandbox. Run them with `sudo -E python -m pytest tests/test_sandbox.py`.

| Attack | Expected result |
|---|---|
| Busy loop | TLE, with CPU time between 1000 and 1300 ms on a 1000 ms limit |
| `sleep` forever | TLE from the wall clock limit |
| Allocate memory until killed | MLE |
| Fork bomb | TLE or RE, stopped by the cgroup pids limit; the host keeps running |
| Write output forever | OLE |
| Write to `/usr`, `/box` (the source dir) or `/` | `EROFS`; only `/tmp` is writable |
| Read `/etc/shadow`, `/root`, a file in the host's `/tmp` | Not present; `/home`, `/var`, `/opt` and `/run` do not exist |
| Read environment variables | Exactly `HOME`, `LANG`, `PATH`; a `JWT_SECRET` set on the host does not leak |
| Connect to 1.1.1.1 or 127.0.0.1 | Fails, the network namespace has no interfaces up |
| List `/proc`, `kill(-1, SIGKILL)` | Sees at most 2 pids; host processes are unaffected |
| `setuid(0)` | Runs as 65534, `setuid` is denied |
| `#include "/dev/urandom"` at compile time | Compiler is stopped (TLE, MLE, RE or OLE) in under 15 s |
| Segfault | RE with `SIGSEGV` recorded |
| Recursion one million frames deep | Allowed; the stack is unlimited, as on most judges |
| Signals ignored by pid 1 | Program is pid 2 and sees no ignored signals |

Not covered yet: syscall filtering (seccomp), and timing side channels between
submissions judged on the same core.
