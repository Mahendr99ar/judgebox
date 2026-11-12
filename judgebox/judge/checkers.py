"""Decide whether a program's output is correct.

Built-in checkers compare in Python. A custom checker is a C++ program the
problem setter writes (for "print any valid answer" problems); it gets three
file arguments, input output answer, and exits 0 for accepted and 1 for wrong.
Any other exit code means the checker itself broke, which is our fault, not
the contestant's.
"""
from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from judgebox.sandbox.runner import Limits, run


@dataclass
class CheckResult:
    accepted: bool
    message: str = ""
    checker_failed: bool = False


def _lines(data: bytes) -> list[bytes]:
    lines = [line.rstrip() for line in data.replace(b"\r\n", b"\n").split(b"\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def exact(output: bytes, answer: bytes) -> CheckResult:
    """Line by line, ignoring trailing spaces and trailing blank lines."""
    got, want = _lines(output), _lines(answer)
    for i, (g, w) in enumerate(zip(got, want, strict=False), 1):
        if g != w:
            return CheckResult(False, f"line {i} differs")
    if len(got) != len(want):
        return CheckResult(False, f"expected {len(want)} lines, got {len(got)}")
    return CheckResult(True)


def tokens(output: bytes, answer: bytes) -> CheckResult:
    got, want = output.split(), answer.split()
    for i, (g, w) in enumerate(zip(got, want, strict=False), 1):
        if g != w:
            return CheckResult(False, f"token {i}: expected {_short(w)}, got {_short(g)}")
    if len(got) != len(want):
        return CheckResult(False, f"expected {len(want)} tokens, got {len(got)}")
    return CheckResult(True)


def floats(output: bytes, answer: bytes, tolerance: float) -> CheckResult:
    """Numbers match within absolute or relative tolerance; other tokens must be equal."""
    got, want = output.split(), answer.split()
    if len(got) != len(want):
        return CheckResult(False, f"expected {len(want)} tokens, got {len(got)}")
    for i, (g, w) in enumerate(zip(got, want, strict=False), 1):
        try:
            gv, wv = float(g), float(w)
        except ValueError:
            if g != w:
                return CheckResult(False, f"token {i}: expected {_short(w)}, got {_short(g)}")
            continue
        if math.isnan(gv) or not math.isclose(gv, wv, rel_tol=tolerance, abs_tol=tolerance):
            return CheckResult(False, f"token {i}: expected {wv}, got {_short(g)}")
    return CheckResult(True)


def custom(checker_binary: Path, workdir: Path, input_path: Path, output_path: Path, answer_path: Path) -> CheckResult:
    box = workdir / "checker-box"
    if box.exists():
        shutil.rmtree(box)
    box.mkdir()
    shutil.copy(checker_binary, box / "checker")
    shutil.copy(input_path, box / "input")
    shutil.copy(output_path, box / "output")
    shutil.copy(answer_path, box / "answer")
    box.chmod(0o755)
    message = workdir / "checker.txt"
    r = run(["./checker", "input", "output", "answer"], box=box, stdout=message,
            limits=Limits(cpu_ms=10_000, memory_kb=512 * 1024))
    text = message.read_text(errors="replace")[:500].strip()
    if r.status == "OK":
        return CheckResult(True, text)
    if r.status == "RE" and r.exit_code == 1:
        return CheckResult(False, text or "wrong answer")
    return CheckResult(False, f"checker {r.status} {r.detail}: {text}", checker_failed=True)


def _short(token: bytes, limit: int = 30) -> str:
    s = token.decode(errors="replace")
    return s if len(s) <= limit else s[:limit] + "..."
