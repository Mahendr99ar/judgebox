"""Judge one submission: compile, run every test, check, stop at the first failure."""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from judgebox.judge import checkers
from judgebox.judge.languages import LANGUAGES, Language
from judgebox.judge.problem import Problem
from judgebox.sandbox.runner import Limits, RunResult, run

# Verdicts a contestant can get. SE (system error) is ours: the worker
# retries it, and it never counts as an attempt in a contest.
VERDICTS = ("AC", "WA", "TLE", "MLE", "OLE", "RE", "CE", "SE")
_RUN_TO_VERDICT = {"TLE": "TLE", "MLE": "MLE", "OLE": "OLE", "RE": "RE", "SANDBOX_ERROR": "SE"}

COMPILE_LIMITS = dict(cpu_ms=15_000, wall_ms=30_000, memory_kb=1024 * 1024, output_kb=1024)
CHECKER_CACHE = Path(os.environ.get("JUDGEBOX_CACHE", "/var/cache/judgebox/checkers"))


@dataclass
class TestResult:
    index: int
    name: str
    verdict: str
    cpu_ms: int
    memory_kb: int
    message: str = ""


@dataclass
class Outcome:
    verdict: str
    cpu_ms: int = 0
    memory_kb: int = 0
    compile_message: str = ""
    message: str = ""
    tests: list[TestResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


Emit = Callable[[dict], None]


def _compile(lang: Language, box: Path, work: Path) -> RunResult | None:
    if lang.compile is None:
        return None
    box.chmod(0o777)  # the compiler runs as nobody and writes its output here
    result = run(list(lang.compile), box=box, writable_box=True, stdout=work / "compile.out",
                 stderr=work / "compile.err", limits=Limits(pids=lang.compile_pids, **COMPILE_LIMITS))
    box.chmod(0o755)
    return result


def compiled_checker(problem: Problem) -> Path:
    """Compile the problem's checker once per problem version, then reuse it."""
    target = CHECKER_CACHE / f"{problem.slug}-{problem.fingerprint()}"
    if target.exists():
        return target
    CHECKER_CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jb-checker-") as tmp:
        box = Path(tmp) / "box"
        box.mkdir()
        shutil.copy(problem.checker_source, box / "main.cpp")
        r = _compile(LANGUAGES["cpp17"], box, Path(tmp))
        if r is None or not r.ok:
            err = (Path(tmp) / "compile.err").read_text(errors="replace")[-2000:]
            raise RuntimeError(f"checker for {problem.slug} does not compile: {err}")
        partial = target.with_suffix(".partial")
        shutil.copy(box / "main", partial)
        partial.replace(target)  # atomic, so two workers never see half a binary
    return target


def judge(problem: Problem, language: str, source: str, emit: Emit = lambda e: None,
          workdir: Path | None = None) -> Outcome:
    lang = LANGUAGES[language]
    own_workdir = workdir is None
    work = Path(tempfile.mkdtemp(prefix="jb-sub-")) if own_workdir else workdir
    try:
        return _judge(problem, lang, source, emit, work)
    finally:
        if own_workdir:
            shutil.rmtree(work, ignore_errors=True)


def _judge(problem: Problem, lang: Language, source: str, emit: Emit, work: Path) -> Outcome:
    box = work / "box"
    box.mkdir()
    (box / lang.source_file).write_text(source)

    emit({"type": "compiling"})
    c = _compile(lang, box, work)
    if c is not None and not c.ok:
        if c.status == "SANDBOX_ERROR":
            return Outcome("SE", message=c.detail)
        message = (work / "compile.err").read_text(errors="replace")[-4000:]
        if c.status != "RE":
            message = f"compiler stopped: {c.status} {c.detail}\n{message}"
        emit({"type": "compiled", "ok": False})
        return Outcome("CE", compile_message=message)
    emit({"type": "compiled", "ok": True})

    checker_bin = compiled_checker(problem) if problem.checker == "custom" else None
    limits = Limits(
        cpu_ms=int(problem.time_limit_ms * lang.time_factor),
        memory_kb=(problem.memory_mb + lang.extra_memory_mb) * 1024,
        pids=lang.pids,
    )
    argv = lang.run_argv(problem.memory_mb)
    out = work / "output.txt"
    outcome = Outcome("AC")

    for i, test in enumerate(problem.tests, 1):
        r = run(argv, box=box, limits=limits, stdin=test.input, stdout=out)
        verdict = _RUN_TO_VERDICT.get(r.status, "AC")
        message = r.detail
        if verdict == "AC":
            check = _check(problem, checker_bin, work, test.input, out, test.answer)
            if check.checker_failed:
                verdict, message = "SE", check.message
            elif not check.accepted:
                verdict, message = "WA", check.message

        result = TestResult(i, test.name, verdict, r.cpu_ms, r.memory_kb, message)
        outcome.tests.append(result)
        outcome.cpu_ms = max(outcome.cpu_ms, r.cpu_ms)
        outcome.memory_kb = max(outcome.memory_kb, r.memory_kb)
        emit({"type": "test", **asdict(result), "total": len(problem.tests)})

        if verdict != "AC":
            outcome.verdict = verdict
            outcome.message = f"test {i}: {message}" if message else f"test {i}"
            break

    return outcome


def _check(problem: Problem, checker_bin: Path | None, work: Path, input_path: Path,
           output_path: Path, answer_path: Path) -> checkers.CheckResult:
    if problem.checker == "custom":
        return checkers.custom(checker_bin, work, input_path, output_path, answer_path)
    output = output_path.read_bytes()
    answer = answer_path.read_bytes()
    if problem.checker == "exact":
        return checkers.exact(output, answer)
    if problem.checker == "float":
        return checkers.floats(output, answer, problem.float_tolerance)
    return checkers.tokens(output, answer)
