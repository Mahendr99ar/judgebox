# Design decisions

Notes on the choices that were not obvious, and what they cost.

## Sandbox

### Namespaces and cgroups instead of Docker per submission

Starting a container per run costs hundreds of milliseconds and needs the
Docker socket inside the worker, which is root on the host. The sandbox instead
calls `unshare` for mount, pid, network, IPC and UTS namespaces, builds a root
from a tmpfs plus read-only bind mounts of `/usr`, `/lib` and a short allowlist
from `/etc`, then `chroot`s into it. The program runs as uid 65534 with
`PR_SET_NO_NEW_PRIVS` set.

`chroot` rather than `pivot_root`: the process inside has no capabilities and
cannot call `chroot` again, which is the classic escape. `pivot_root` would be
stricter and is a reasonable next step.

### The program is pid 2, not pid 1

The first version ran the submission as pid 1 of its pid namespace. The kernel
does not deliver signals with a default action to a namespace's init, so
`SIGXCPU` from `RLIMIT_CPU` was dropped and a 1000 ms limit only stopped at the
hard limit, around 2990 ms. A tiny reaper now sits at pid 1 and the program is
pid 2, where signals behave normally. A test checks that the program sees
`SigIgn: 0000000000000000`.

### CPU time comes from the cgroup, enforced by a watchdog

`RLIMIT_CPU` has one-second granularity and only covers one process. The reaper
polls the cgroup's CPU counter every 10 ms and kills the whole group at the
limit plus 50 ms. On a 1000 ms limit a busy loop is stopped at about 1059 ms.

The program joins the cgroup at the last moment before `execve`, using file
descriptors opened earlier as root. Joining at fork time billed the sandbox's
own setup to the submission: `/bin/true` measured 12 ms. Now it measures about 1 ms.

### Output limit is checked by size, not only by signal

`RLIMIT_FSIZE` raises `SIGXFSZ`, but CPython ignores that signal and gets
`EFBIG` instead, so a Python program writing forever ended as a runtime error.
The runner also treats an output file that reached the limit as OLE.

### No seccomp filter yet

The namespaces remove network and the filesystem is read-only, but every
syscall is still allowed. A seccomp allowlist is the main missing layer.

### Known cost: about 67 ms per run

Each run starts `python -m judgebox.sandbox.init`, and Python's startup is most
of that. A problem with 50 tests pays it 50 times. A forkserver that stays
loaded, or rewriting `init` in C, would remove most of it.

## Judging

- TLE is final. A submission that timed out once will time out again; retrying
  it only wastes a worker. Only SE (a fault on our side) is retried.
- Judging stops at the first failing test, like Codeforces, so the verdict says
  which test failed.
- Custom checkers are compiled once per problem fingerprint and cached. They run
  inside the same sandbox as submissions because problem setters make mistakes too.
- `<bits/stdc++.h>` is precompiled with the exact judge flags. Judging a C++
  A+B that includes it went from 1.66 s to 0.75 s.

## Queue

Redis Streams with a consumer group rather than a list or BullMQ-style job:

- A message stays pending until the worker acknowledges it, so a worker killed
  mid-judge does not lose the submission. Another worker takes it over with
  `XAUTOCLAIM` once it has been idle for `VISIBILITY_MS`.
- A long judge refreshes its claim on every progress event, so it is not
  stolen while still running.
- After three deliveries the message moves to `jb:dead` and the submission is
  marked SE instead of looping forever.
- Delivery is at least once, so finishing is idempotent: the worker locks the
  row and only writes if the status is not already `done`.
- If Redis loses data, a sweeper re-enqueues submissions stuck in `queued`, but
  only when the stream is idle, so it cannot double up during a burst.

Progress events go to a Redis list with a sequence number and to pub/sub. The
SSE endpoint replays the list first, so a page opened halfway through judging
still shows every test.

## Contests

The scoreboard is computed from submission rows on each request rather than
kept as counters. Counters drift when a submission is rejudged; rows do not.
For a club-sized contest this is cheap.

ICPC rules: penalty is minutes to first AC plus 20 per earlier rejected try.
CE and SE are not counted as tries. During the freeze, other people's
submissions after `freeze_at` show as pending, your own stay visible, and
admins see everything until they unfreeze.

## API

- The role is read from the database on each request, never from the token or
  the request body. Request models forbid unknown fields, so `"role": "admin"`
  in a registration body is a 422.
- Another user's submission returns 404, not 403, so ids cannot be probed.
- Submissions are rate limited per user per minute with a Redis counter.
