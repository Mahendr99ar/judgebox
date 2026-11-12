# judgebox

An online judge for programming contests. Submissions in C++17, Python 3 and
Java 21 are compiled and run in a sandbox built from Linux namespaces and
cgroups, judged by workers that pull from a Redis Stream, and streamed back to
the browser test by test. Contests use ICPC scoring with a scoreboard freeze.

## What it does

- **Sandbox.** Each run gets its own mount, pid, network, IPC and UTS
  namespaces, a read-only root, uid 65534, and a cgroup for memory, pids and
  CPU time. Verdicts: AC, WA, TLE, MLE, OLE, RE, CE, SE.
- **Workers.** Any number of them read one Redis Stream as a consumer group. A
  worker that dies mid-judge has its submission picked up by another; a
  submission that keeps failing ends in a dead-letter stream as SE.
- **Live results.** The submission page shows each test as it finishes, over
  server-sent events. Reloading halfway through replays what already happened.
- **Problems as folders.** `problem.toml`, `statement.md` and `tests/*.in` /
  `*.ans`. Checkers: exact lines, tokens, floats with tolerance, or a custom
  C++ checker.
- **Contests.** ICPC penalty, freeze before the end, admin unfreeze.

## Numbers

Measured on a 2 vCPU VM with the sample problems in `problems/`.

| | |
|---|---|
| CPU time at which a busy loop is stopped, 1000 ms limit | about 1059 ms |
| Sandbox overhead per run (wall time) | about 67 ms |
| Judge a C++ A+B that includes `<bits/stdc++.h>` | 1.66 s, 0.75 s with the precompiled header |
| Burst of 60 submissions from 20 users, 2 workers | 115 per minute, verdict p50 8.12 s, p95 9.81 s |

The latency in the last row is mostly time waiting in the queue during the
burst. `scripts/loadtest.py` reproduces it.

## Run it

With Docker:

```bash
cp .env.example .env          # fill in JWT_SECRET and POSTGRES_PASSWORD
docker compose up --build
docker compose exec api python -m judgebox.cli create-admin root
```

Then open http://localhost:8000. Deploying on a VM is covered in
[deploy/oracle-vm.md](deploy/oracle-vm.md).

Without Docker you need Postgres 16, Redis 7, g++, OpenJDK 21, and root for the
workers:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
sudo scripts/build_pch.sh
export JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export DATABASE_URL=postgresql+psycopg://postgres@localhost/judgebox
.venv/bin/uvicorn judgebox.api.app:app --port 8000 &
sudo -E .venv/bin/python -m judgebox.worker.main
```

## Tests

```bash
sudo -E .venv/bin/python -m pytest
```

60 tests. The sandbox ones run real attacks: fork bombs, memory bombs, output
floods, reading host files and environment, opening sockets, `setuid(0)`.
The full list is in [docs/security.md](docs/security.md). Tests that need
Postgres and Redis skip when those are not running.

## Layout

```
judgebox/sandbox/   namespaces, cgroups, resource limits, verdict classification
judgebox/judge/     problem packages, languages, checkers, the judge loop
judgebox/worker/    stream consumer, retries, dead letters, sweeper
judgebox/api/       FastAPI app, auth, scoreboard
web/                single-page UI, no build step
problems/           sample problems
```

Why things are built the way they are, including what is still missing (no
seccomp filter yet, per-run Python startup cost): [docs/decisions.md](docs/decisions.md).

## Limits

- Linux only. Workers need root, or a privileged container.
- Developed and measured on a cgroup v1 host. The v2 backend, the Docker image
  and the CI workflow are written but have not been run yet.
- One worker judges one submission at a time.
