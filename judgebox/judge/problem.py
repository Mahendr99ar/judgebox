"""Problem packages on disk.

    problems/<slug>/
        problem.toml      title, limits, checker
        statement.md
        tests/01.in  tests/01.ans  tests/02.in ...
        checker.cpp       only when checker = "custom"

Tests run in file-name order, so name the small ones first: a wrong
solution should fail on 01, not after the 2-second stress test.
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

CHECKERS = ("exact", "tokens", "float", "custom")


class ProblemError(ValueError):
    pass


@dataclass(frozen=True)
class TestCase:
    name: str
    input: Path
    answer: Path


@dataclass(frozen=True)
class Problem:
    slug: str
    title: str
    time_limit_ms: int
    memory_mb: int
    checker: str
    float_tolerance: float
    public: bool
    root: Path
    tests: tuple[TestCase, ...]

    @property
    def statement(self) -> str:
        p = self.root / "statement.md"
        return p.read_text() if p.exists() else ""

    @property
    def checker_source(self) -> Path:
        return self.root / "checker.cpp"

    def fingerprint(self) -> str:
        """Changes whenever a test or the checker changes; used to cache compiled checkers."""
        h = hashlib.sha256()
        for f in sorted(self.root.rglob("*")):
            if f.is_file():
                h.update(f.relative_to(self.root).as_posix().encode())
                h.update(f.read_bytes())
        return h.hexdigest()[:16]


def load(root: Path) -> Problem:
    meta_path = root / "problem.toml"
    if not meta_path.exists():
        raise ProblemError(f"{root.name}: problem.toml missing")
    meta = tomllib.loads(meta_path.read_text())

    checker = meta.get("checker", "tokens")
    if checker not in CHECKERS:
        raise ProblemError(f"{root.name}: unknown checker {checker!r}, expected one of {CHECKERS}")
    if checker == "custom" and not (root / "checker.cpp").exists():
        raise ProblemError(f"{root.name}: checker = 'custom' but checker.cpp is missing")

    tests = []
    for inp in sorted((root / "tests").glob("*.in")):
        ans = inp.with_suffix(".ans")
        if not ans.exists():
            raise ProblemError(f"{root.name}: {inp.name} has no matching .ans file")
        tests.append(TestCase(inp.stem, inp, ans))
    if not tests:
        raise ProblemError(f"{root.name}: no tests")

    time_limit = int(meta.get("time_limit_ms", 1000))
    memory = int(meta.get("memory_mb", 256))
    if not (100 <= time_limit <= 10_000) or not (16 <= memory <= 1024):
        raise ProblemError(f"{root.name}: limits out of range ({time_limit} ms, {memory} MB)")

    return Problem(
        slug=root.name,
        title=meta.get("title", root.name),
        time_limit_ms=time_limit,
        memory_mb=memory,
        checker=checker,
        float_tolerance=float(meta.get("float_tolerance", 1e-6)),
        # Problems written for a contest stay hidden from the practice list.
        public=bool(meta.get("public", True)),
        root=root,
        tests=tuple(tests),
    )


def load_all(directory: Path) -> dict[str, Problem]:
    return {p.slug: p for p in (load(d) for d in sorted(directory.iterdir()) if (d / "problem.toml").exists())}
