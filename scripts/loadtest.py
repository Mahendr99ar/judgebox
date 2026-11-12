"""Submit a burst of solutions through the API and measure time to verdict.

    python scripts/loadtest.py --base http://localhost:8000 --users 20 --per-user 3

Creates throwaway users (u<run>-<n>), so the per-user rate limit is not the
bottleneck. Prints throughput and latency percentiles; latency is from the
POST returning to the verdict being stored.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
import uuid

import httpx

SOLUTIONS = [
    ("a-plus-b", "python3", "a, b = map(int, input().split())\nprint(a + b)"),
    (
        "a-plus-b",
        "cpp17",
        '#include <cstdio>\nint main(){long long a,b;scanf("%lld %lld",&a,&b);printf("%lld\\n",a+b);}',
    ),
    ("circle-area", "python3", "import math\nr = float(input())\nprint(math.pi * r * r)"),
    ("sort-numbers", "cpp17", "#include <bits/stdc++.h>\nint main(){int n;scanf(\"%d\",&n);std::vector<int>a(n);"
     "for(auto&x:a)scanf(\"%d\",&x);std::sort(a.begin(),a.end());for(int x:a)printf(\"%d \",x);}"),
    ("a-plus-b", "python3", "print(0)"),  # WA after 1 test
]


async def user_session(client: httpx.AsyncClient, name: str, count: int, results: list) -> None:
    creds = {"username": name, "password": "loadtest-password"}
    await client.post("/api/auth/register", json=creds)
    token = (await client.post("/api/auth/login", json=creds)).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(count):
        problem, language, source = random.choice(SOLUTIONS)
        res = await client.post("/api/submissions", headers=headers,
                                json={"problem": problem, "language": language, "source": source})
        res.raise_for_status()
        sid, sent = res.json()["id"], time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            sub = (await client.get(f"/api/submissions/{sid}", headers=headers)).json()
            if sub["status"] == "done":
                results.append((time.monotonic() - sent, sub["verdict"], problem, language))
                break


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--per-user", type=int, default=3)
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    results: list = []
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=args.base, timeout=60) as client:
        await asyncio.gather(*(user_session(client, f"u{run}-{i}", args.per_user, results) for i in range(args.users)))
    elapsed = time.monotonic() - started

    latencies = sorted(r[0] for r in results)
    q = statistics.quantiles(latencies, n=100)
    verdicts: dict[str, int] = {}
    for _, v, _, _ in results:
        verdicts[v] = verdicts.get(v, 0) + 1
    print(f"submissions   {len(results)} in {elapsed:.1f} s  ({len(results) / elapsed * 60:.0f} per minute)")
    print(f"time to verdict  p50 {q[49]:.2f} s   p95 {q[94]:.2f} s   max {latencies[-1]:.2f} s")
    print(f"verdicts      {verdicts}")


if __name__ == "__main__":
    asyncio.run(main())
