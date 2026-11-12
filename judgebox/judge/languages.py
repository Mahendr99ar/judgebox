"""How each language is compiled and run.

Commands use absolute paths because the sandbox has no shell and a minimal
PATH. `{mem}` in a run command is replaced with the problem's memory limit.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    key: str
    name: str
    source_file: str
    compile: tuple[str, ...] | None
    run: tuple[str, ...]
    # The JVM starts a dozen threads and keeps metaspace and code cache outside
    # the heap, so it gets more processes and some memory on top of -Xmx.
    extra_memory_mb: int = 0
    pids: int = 16
    compile_pids: int = 32
    time_factor: float = 1.0

    def run_argv(self, memory_mb: int) -> list[str]:
        return [part.replace("{mem}", str(memory_mb)) for part in self.run]


LANGUAGES: dict[str, Language] = {
    lang.key: lang
    for lang in (
        Language(
            key="cpp17",
            name="C++17 (g++)",
            source_file="main.cpp",
            # -I points at the precompiled <bits/stdc++.h> (scripts/build_pch.sh).
            # GCC skips include dirs that do not exist, so this is safe without it.
            compile=("/usr/bin/g++", "-O2", "-std=gnu++17", "-pipe", "-I/usr/local/lib/judgebox-pch",
                     "-o", "main", "main.cpp"),
            run=("./main",),
        ),
        Language(
            key="python3",
            name="Python 3",
            source_file="main.py",
            # Byte-compiling first turns a syntax error into a compile error
            # instead of a runtime error on test 1.
            compile=("/usr/bin/python3", "-m", "py_compile", "main.py"),
            run=("/usr/bin/python3", "-S", "main.py"),
        ),
        Language(
            key="java21",
            name="Java 21",
            source_file="Main.java",
            compile=("/usr/bin/javac", "-encoding", "UTF-8", "-J-Xmx512m", "Main.java"),
            run=("/usr/bin/java", "-Xmx{mem}m", "-Xss64m", "-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1", "Main"),
            extra_memory_mb=128,
            pids=64,
            compile_pids=128,
            time_factor=2.0,
        ),
    )
}
