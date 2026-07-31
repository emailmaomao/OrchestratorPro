"""Shared fixtures for builder tests.

Every test here is offline and touches no real build tool: commands are run
through a scripted process runner, and the "projects" are small trees written
into a temporary directory. What is real is the filesystem — digests, artifact
collection, and cache verification are all about what is actually on disk, and
mocking that away would test nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine, Mapping
from pathlib import Path
from typing import Any, TypeVar

import pytest

from orchestrator.builder.analysis import DependencyAnalyzer, ProjectAnalyzer, UnitGraph
from orchestrator.builder.cache import MemoryCache
from orchestrator.builder.model import BuildUnit, ProjectLayout, SourceFile, digest_text
from orchestrator.core.storage import Database
from orchestrator.test_runner.execution import ProcessResult, ScriptedRunner

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


def unit(name: str, **kwargs: Any) -> BuildUnit:
    """A build unit with test-friendly defaults."""
    kwargs.setdefault("command", f"build {name}")
    kwargs.setdefault("sources", (name,))
    return BuildUnit(name=name, **kwargs)


def layout(
    *units: BuildUnit,
    sources: Mapping[str, str] | None = None,
    root: Path | None = None,
    **kwargs: Any,
) -> ProjectLayout:
    """A layout over in-memory source contents.

    Args:
        *units: The build units.
        sources: Path to file content. Defaults to one file per unit.
        root: The project root. Only matters for artifact verification.
    """
    contents = dict(
        sources
        if sources is not None
        else {f"{u.name}/main.py": f"# {u.name}\n" for u in units}
    )
    return ProjectLayout(
        root=root or Path("/project"),
        units=tuple(units),
        sources={
            path: SourceFile(path=path, digest=digest_text(text), size=len(text))
            for path, text in contents.items()
        },
        **kwargs,
    )


def graph_of(*units: BuildUnit) -> UnitGraph:
    """A validated graph over the given units."""
    return UnitGraph(units)


def ok(stdout: str = "", **kwargs: Any) -> ProcessResult:
    """A process that succeeded."""
    return ProcessResult(exit_code=0, stdout=stdout, **kwargs)


def bad(stdout: str = "", *, exit_code: int = 1, **kwargs: Any) -> ProcessResult:
    """A process that failed."""
    return ProcessResult(exit_code=exit_code, stdout=stdout, **kwargs)


def scripted(*results: ProcessResult, default: ProcessResult | None = None) -> ScriptedRunner:
    """A process runner returning prepared results."""
    return ScriptedRunner(results, default=default or ProcessResult(exit_code=0))


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Write a small project tree and return its root."""
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


class FakeClock:
    """A monotonic clock that advances only when told."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


@pytest.fixture
def project(tmp_dir: Path) -> Path:
    """An empty project directory."""
    root = tmp_dir / "project"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def cache() -> MemoryCache:
    """An empty in-process cache."""
    return MemoryCache()


@pytest.fixture
def db() -> Database:
    """A migrated in-memory database."""
    database = Database.in_memory()
    database.migrate()
    return database


@pytest.fixture
def analyzers() -> tuple[ProjectAnalyzer, DependencyAnalyzer]:
    """The two analyzers, with inference on."""
    return ProjectAnalyzer(), DependencyAnalyzer()


PYTHON_PROJECT: Mapping[str, str] = {
    "pyproject.toml": "[project]\nname = 'demo'\n",
    "core/__init__.py": "",
    "core/util.py": "def helper() -> int:\n    return 1\n",
    "app/__init__.py": "",
    "app/main.py": "import core\n\n\ndef run() -> int:\n    return core.util.helper()\n",
}

GCC_OUTPUT = """\
src/main.c: In function 'main':
src/main.c:12:5: error: 'undeclared' undeclared (first use in this function)
src/main.c:14:9: warning: unused variable 'x' [-Wunused-variable]
"""

TSC_OUTPUT = """\
src/app.ts(30,12): error TS2345: Argument of type 'string' is not assignable.
"""

RUST_OUTPUT = """\
error[E0308]: mismatched types
  --> src/lib.rs:42:17
   |
42 |     let x: u8 = "no";
"""

PYTHON_TRACEBACK = """\
Traceback (most recent call last):
  File "build.py", line 8, in <module>
    main()
ImportError: cannot import name 'missing' from 'core'
"""

MISSING_TOOL_OUTPUT = "/bin/sh: 1: cargo: command not found"
