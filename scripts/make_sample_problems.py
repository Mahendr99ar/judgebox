"""Generate the sample problems in problems/. Deterministic: same seed, same files."""
from __future__ import annotations

import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "problems"


def write(slug: str, toml: str, statement: str, tests: list[tuple[str, str]], checker: str | None = None) -> None:
    d = ROOT / slug
    (d / "tests").mkdir(parents=True, exist_ok=True)
    (d / "problem.toml").write_text(toml.strip() + "\n")
    (d / "statement.md").write_text(statement.strip() + "\n")
    for i, (inp, ans) in enumerate(tests, 1):
        (d / "tests" / f"{i:02d}.in").write_text(inp)
        (d / "tests" / f"{i:02d}.ans").write_text(ans)
    if checker:
        (d / "checker.cpp").write_text(checker.strip() + "\n")


def a_plus_b(rng: random.Random) -> None:
    tests = [("2 3\n", "5\n"), ("-7 7\n", "0\n")]
    for _ in range(4):
        a, b = rng.randint(-10**18, 10**18), rng.randint(-10**18, 10**18)
        tests.append((f"{a} {b}\n", f"{a + b}\n"))
    write("a-plus-b", 'title = "A + B"\ntime_limit_ms = 1000\nmemory_mb = 64\nchecker = "tokens"', """
# A + B

Read two integers a and b (|a|, |b| <= 10^18) and print a + b.

Watch the range: the sum does not fit in a 32-bit integer.
""", tests)


def sort_numbers(rng: random.Random) -> None:
    tests = []
    for n in (5, 1000, 50_000, 300_000):
        xs = [rng.randint(-10**9, 10**9) for _ in range(n)]
        tests.append((f"{n}\n{' '.join(map(str, xs))}\n", " ".join(map(str, sorted(xs))) + "\n"))
    write("sort-numbers", 'title = "Sort the numbers"\ntime_limit_ms = 1000\nmemory_mb = 256\nchecker = "tokens"', """
# Sort the numbers

The first line has n (1 <= n <= 3 * 10^5). The second line has n integers.
Print them in non-decreasing order on one line.

An O(n^2) sort will not finish in time on the last test.
""", tests)


def circle_area(rng: random.Random) -> None:
    tests = []
    for r in (1, 2.5, 1000, 0.001) + tuple(round(rng.uniform(0, 1e4), 3) for _ in range(3)):
        tests.append((f"{r}\n", f"{math.pi * r * r:.9f}\n"))
    toml = 'title = "Circle area"\ntime_limit_ms = 1000\nmemory_mb = 64\nchecker = "float"\nfloat_tolerance = 1e-6'
    write("circle-area", toml, """
# Circle area

Print the area of a circle with radius r (0 < r <= 10^4).
Answers within 10^-6 absolute or relative error are accepted.
""", tests)


def two_sum(rng: random.Random) -> None:
    tests = []
    for n in (4, 10, 1000, 200_000):
        xs = [rng.randint(1, 10**9) for _ in range(n)]
        i, j = rng.sample(range(n), 2)
        target = xs[i] + xs[j]
        tests.append((f"{n} {target}\n{' '.join(map(str, xs))}\n", f"{min(i, j) + 1} {max(i, j) + 1}\n"))
    checker = r'''
// Any pair of distinct 1-based indices whose values add up to the target is accepted.
#include <cstdio>
#include <vector>
int main(int argc, char** argv) {
    FILE* in = fopen(argv[1], "r");
    FILE* out = fopen(argv[2], "r");
    long long n, target;
    if (fscanf(in, "%lld %lld", &n, &target) != 2) return 3;
    std::vector<long long> a(n);
    for (auto& x : a) fscanf(in, "%lld", &x);
    long long i, j;
    if (fscanf(out, "%lld %lld", &i, &j) != 2) { puts("expected two indices"); return 1; }
    if (i < 1 || j < 1 || i > n || j > n || i == j) { puts("indices out of range or equal"); return 1; }
    if (a[i - 1] + a[j - 1] != target) { puts("values do not add up to the target"); return 1; }
    return 0;
}
'''
    write("two-sum", 'title = "Two sum"\ntime_limit_ms = 1000\nmemory_mb = 128\nchecker = "custom"', """
# Two sum

The first line has n and t. The second line has n positive integers.
Print two different 1-based indices i and j with a_i + a_j = t.
A solution always exists; if there are several, print any of them.
""", tests, checker)


if __name__ == "__main__":
    rng = random.Random(20261112)
    a_plus_b(rng)
    sort_numbers(rng)
    circle_area(rng)
    two_sum(rng)
    print("written to", ROOT)
