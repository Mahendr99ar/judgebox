# Deploying on a single VM

Tested layout: one Ubuntu 24.04 VM running everything through Docker Compose.
Oracle Cloud's always-free Ampere instance is enough for a club contest.

## 1. Prepare the host

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER   # log out and back in
```

Workers need cgroup v2 with the memory, pids and cpu controllers. Ubuntu 24.04
uses v2 by default. Check with:

```bash
stat -fc %T /sys/fs/cgroup        # should print cgroup2fs
cat /sys/fs/cgroup/cgroup.controllers
```

## 2. Configure

```bash
git clone <your repo> judgebox && cd judgebox
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into JWT_SECRET
python3 -c "import secrets; print(secrets.token_hex(16))"   # paste into POSTGRES_PASSWORD
```

## 3. Start

```bash
docker compose up -d --build
docker compose exec api python -m judgebox.cli create-admin root
```

The API listens on port 8000. Put Caddy or nginx in front of it for HTTPS:

```
judge.example.com {
    reverse_proxy localhost:8000
}
```

Open port 443 in both the VM firewall (`ufw` or `iptables`) and the Oracle
security list. Do not expose 5432 or 6379.

## 4. Operate

- More workers: `docker compose up -d --scale worker=4`. One worker judges one
  submission at a time, so scale to the number of cores you can spare.
- Add or edit problems in `problems/`, then call `POST /api/admin/problems/sync`
  or run `docker compose exec api python -m judgebox.cli sync-problems`.
- Queue depth: `GET /api/admin/queue`.
- Submissions that failed three deliveries land in the `jb:dead` stream. Inspect
  with `docker compose exec redis redis-cli XRANGE jb:dead - +`.

## Why the worker is privileged

The sandbox creates namespaces, mounts a tmpfs root and makes cgroups. Docker's
default seccomp profile and capability set block all three. `privileged: true`
is the simple answer; it is limited to the worker containers, and untrusted code
inside them still runs as uid 65534 in its own namespaces with no network.
Running workers directly on the host as a systemd service is the alternative if
you would rather not give a container that access.
