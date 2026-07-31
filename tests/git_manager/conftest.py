"""Shared fixtures for git_manager tests.

Two layers, deliberately:

**Mocked runner.** :class:`FakeGitRunner` scripts Git's replies, so guard rules,
name parsing, and output translation are tested without a repository at all.

**Real temporary repositories.** ``ORCHESTRATOR_PRO_SPEC`` §11 singles this
package out: *"Real Git against temporary repositories. This is the one place
where mocking would test nothing."* Worktree semantics, merge conflicts, and
index state are precisely the things a mock would get wrong in the same way the
implementation does. Those tests run real Git — but only ever inside a
throwaway directory created for the test and removed afterwards. Nothing runs
against the project's own repository.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Coroutine, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pytest

from orchestrator.git_manager.repo import GitRepository, GitResult, SubprocessGitRunner

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


def git_available() -> bool:
    """Whether a usable ``git`` executable is on PATH."""
    if shutil.which("git") is None:
        return False
    probe = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


requires_git = pytest.mark.skipif(
    not git_available(), reason="a real git executable is required"
)


class FakeGitRunner:
    """A scripted Git runner. Records invocations; returns what it was told.

    Keys are the joined argument string, matched exactly, with a prefix fallback
    so a test can script ``"rev-parse"`` without enumerating every variant.
    """

    def __init__(
        self,
        responses: Mapping[str, GitResult | tuple[int, str, str]] | None = None,
        *,
        default: GitResult | tuple[int, str, str] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default if default is not None else (0, "", "")
        self.calls: list[tuple[str, ...]] = []
        self.cwds: list[Path] = []

    def script(self, key: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Add or replace one scripted reply."""
        self.responses[key] = (returncode, stdout, stderr)

    async def run(
        self, args: Sequence[str], *, cwd: Path, timeout: float = 120.0
    ) -> GitResult:
        """Record the invocation and return the scripted reply."""
        argv = tuple(args)
        self.calls.append(argv)
        self.cwds.append(cwd)

        joined = " ".join(argv)
        scripted = self.responses.get(joined)
        if scripted is None:
            for key, value in self.responses.items():
                if joined.startswith(key):
                    scripted = value
                    break
        if scripted is None:
            scripted = self.default

        if isinstance(scripted, GitResult):
            return scripted
        returncode, stdout, stderr = scripted
        return GitResult(args=argv, returncode=returncode, stdout=stdout, stderr=stderr)

    @property
    def last(self) -> tuple[str, ...]:
        """The most recent invocation."""
        assert self.calls, "no git command was run"
        return self.calls[-1]

    def ran(self, *fragment: str) -> bool:
        """Whether any invocation began with ``fragment``."""
        return any(call[: len(fragment)] == fragment for call in self.calls)

    def calls_matching(self, *fragment: str) -> list[tuple[str, ...]]:
        """Every invocation beginning with ``fragment``."""
        return [call for call in self.calls if call[: len(fragment)] == fragment]


@pytest.fixture
def fake_runner() -> FakeGitRunner:
    """A scripted Git runner."""
    return FakeGitRunner()


@pytest.fixture
def fake_repo(fake_runner: FakeGitRunner, tmp_dir: Path) -> GitRepository:
    """A repository backed by the scripted runner."""
    return GitRepository(tmp_dir / "repo", runner=fake_runner)


# --------------------------------------------------------------------------- #
# Real temporary repositories
# --------------------------------------------------------------------------- #


class TempRepo:
    """A throwaway Git repository, torn down with the test."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.path = base / "repo"
        self.path.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "tester@example.com")
        self._git("config", "user.name", "Tester")
        self._git("config", "commit.gpgsign", "false")
        self.write("README.md", "initial\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "initial commit")

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run a raw Git command for test setup."""
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.path),
            capture_output=True,
            text=True,
            check=False,
        )

    def write(self, relative: str, content: str, *, cwd: Path | None = None) -> Path:
        """Write a file inside the repository (or another worktree)."""
        target = (cwd or self.path) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit_file(
        self, relative: str, content: str, message: str, *, cwd: Path | None = None
    ) -> str:
        """Write, stage, and commit one file; return the resulting SHA."""
        self.write(relative, content, cwd=cwd)
        self._git("add", "-A", cwd=cwd)
        self._git("commit", "-q", "-m", message, cwd=cwd)
        return self._git("rev-parse", "HEAD", cwd=cwd).stdout.strip()

    def branch(self, name: str, start: str = "HEAD") -> None:
        """Create a branch."""
        self._git("branch", name, start)

    def sha(self, ref: str = "HEAD") -> str:
        """Resolve a ref."""
        return self._git("rev-parse", ref).stdout.strip()

    def repository(self) -> GitRepository:
        """Return a :class:`GitRepository` bound to this temporary repository."""
        return GitRepository(self.path, runner=SubprocessGitRunner())

    @property
    def worktree_root(self) -> Path:
        """A directory for worktrees, inside the throwaway base."""
        return self.base / "worktrees"


@pytest.fixture
def temp_repo() -> Iterator[TempRepo]:
    """A real, throwaway Git repository.

    Created under a fresh temporary directory and deleted afterwards, so no real
    Git operation ever touches anything outside it.
    """
    with tempfile.TemporaryDirectory(prefix="orchestratorpro-git-") as name:
        yield TempRepo(Path(name))
