"""Repository access and the single audited chokepoint for Git operations.

Every Git invocation in OrchestratorPro passes through :meth:`GitRepository.run`,
and every destructive one is classified before it executes (NFR-3.6). That is
not defence against a malicious agent so much as against an ordinary bug: a
harness that can force-push is a harness that eventually will.

Three classes of operation:

``allowed``
    Reads and ordinary writes. Executed without ceremony.
``authorized``
    Destructive but legitimate — deleting a branch, removing a worktree.
    Requires ``authorize=True`` at the call site, so the intent is visible in
    the code that wants it.
``forbidden``
    Force-pushes and history rewrites. Refused unconditionally. There is no
    flag that permits them, because the spec has no case that needs them
    (``ORCHESTRATOR_PRO_SPEC`` §4.1).

The runner itself is a seam. :class:`SubprocessGitRunner` shells out; tests
inject a scripted runner and never touch a real repository, except in the
integration tests, which create their own throwaway ones.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from orchestrator.core.events import OrchestratorError

__all__ = [
    "DirtyRepositoryError",
    "ForbiddenOperationError",
    "GitCommandError",
    "GitError",
    "GitRepository",
    "GitResult",
    "GitRunner",
    "SubprocessGitRunner",
    "classify_operation",
]

_DEFAULT_TIMEOUT: Final = 120.0


class GitError(OrchestratorError):
    """A Git operation failed."""

    code = "git"
    retryable = False


class GitCommandError(GitError):
    """A Git command exited non-zero."""

    code = "git_command"
    retryable = False

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(args)} exited {returncode}: {stderr.strip()}",
            detail={"args": list(args), "returncode": returncode, "stderr": stderr},
        )
        self.args_run = tuple(args)
        self.returncode = returncode
        self.stderr = stderr


class NotARepositoryError(GitError):
    """The path is not inside a Git repository."""

    code = "not_a_repository"
    retryable = False


class DirtyRepositoryError(GitError):
    """The repository has uncommitted changes and a run refuses to start."""

    code = "dirty_repository"
    retryable = False


class ForbiddenOperationError(GitError):
    """A destructive operation was refused.

    Either it is never permitted, or it needs explicit authorization that the
    call site did not give.
    """

    code = "forbidden_git_operation"
    retryable = False


@dataclass(frozen=True, slots=True)
class GitResult:
    """The outcome of one Git invocation."""

    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.returncode == 0

    @property
    def out(self) -> str:
        """Standard output with surrounding whitespace removed."""
        return self.stdout.strip()

    def lines(self) -> tuple[str, ...]:
        """Standard output split into non-empty lines."""
        return tuple(line for line in self.stdout.splitlines() if line.strip())


class GitRunner(Protocol):
    """Executes a Git command. The seam that keeps tests off real repositories."""

    async def run(
        self, args: Sequence[str], *, cwd: Path, timeout: float = _DEFAULT_TIMEOUT
    ) -> GitResult:
        """Run ``git <args>`` in ``cwd`` and return the result."""
        ...


class SubprocessGitRunner:
    """Runs Git as a subprocess.

    Blocking work goes through :func:`asyncio.to_thread`, per ``CLAUDE.md`` — a
    blocking call on the event loop would stall every other attempt in flight.
    """

    __slots__ = ("_executable",)

    def __init__(self, executable: str = "git") -> None:
        self._executable = executable

    async def run(
        self, args: Sequence[str], *, cwd: Path, timeout: float = _DEFAULT_TIMEOUT
    ) -> GitResult:
        """Execute the command and capture its output."""
        argv = [self._executable, *args]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError(
                f"the git executable {self._executable!r} was not found",
                detail={"executable": self._executable},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git {' '.join(args)} timed out after {timeout}s",
                detail={"args": list(args), "timeout": timeout},
            ) from exc
        return GitResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


def classify_operation(args: Sequence[str]) -> str:
    """Classify a Git invocation as ``allowed``, ``authorized``, or ``forbidden``.

    Args:
        args: The arguments that would follow ``git``.

    Returns:
        The classification.
    """
    if not args:
        return "forbidden"

    command = args[0]
    rest = list(args[1:])
    flags = set(rest)

    # Never permitted: force-pushing and rewriting history. The spec has no
    # case that needs either, so there is no flag that enables them.
    if command == "push" and flags & {"--force", "-f", "--force-with-lease"}:
        return "forbidden"
    if command in {"filter-branch", "rebase"}:
        return "forbidden"
    if command == "commit" and "--amend" in flags:
        return "forbidden"
    if command == "reset" and "--hard" in flags:
        return "forbidden"
    if command == "update-ref" and "-d" in flags:
        return "authorized"

    # Destructive but legitimate: must be asked for explicitly.
    if command == "push" and flags & {"--delete", "-d"}:
        return "authorized"
    if command == "branch" and flags & {"-d", "-D", "--delete"}:
        return "authorized"
    if command == "worktree" and rest and rest[0] in {"remove", "prune"}:
        return "authorized"
    if command == "clean":
        return "authorized"

    return "allowed"


class GitRepository:
    """One Git repository, and the guarded gateway to it."""

    __slots__ = ("_path", "_runner")

    def __init__(self, path: Path, *, runner: GitRunner | None = None) -> None:
        """Bind to a repository.

        Args:
            path: The repository's working-tree root.
            runner: Executes commands. Defaults to a real subprocess runner.
        """
        self._path = Path(path)
        self._runner = runner or SubprocessGitRunner()

    @property
    def path(self) -> Path:
        """The repository root."""
        return self._path

    @property
    def runner(self) -> GitRunner:
        """The underlying command runner."""
        return self._runner

    # ------------------------------------------------------------- execution

    async def run(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        authorize: bool = False,
    ) -> GitResult:
        """Run a Git command through the guard.

        Args:
            *args: Arguments following ``git``.
            cwd: Directory to run in. Defaults to the repository root, so a
                worktree must pass its own path explicitly.
            check: Raise on a non-zero exit.
            authorize: Permit a destructive-but-legitimate operation.

        Returns:
            The command's result.

        Raises:
            ForbiddenOperationError: If the operation is never permitted, or
                needs authorization that was not given.
            GitCommandError: If ``check`` and the command failed.
        """
        classification = classify_operation(args)
        if classification == "forbidden":
            raise ForbiddenOperationError(
                f"git {' '.join(args)} is never permitted: OrchestratorPro does not "
                "force-push or rewrite history",
                detail={"args": list(args)},
            )
        if classification == "authorized" and not authorize:
            raise ForbiddenOperationError(
                f"git {' '.join(args)} is destructive and requires explicit "
                "authorization from the calling code",
                detail={"args": list(args)},
            )

        result = await self._runner.run(list(args), cwd=cwd or self._path)
        if check and not result.ok:
            raise GitCommandError(result.args, result.returncode, result.stderr)
        return result

    # ----------------------------------------------------------------- reads

    async def diff_branch(
        self, branch: str, *, base: str = "", context: int = 3
    ) -> str:
        """Return the patch a branch introduced.

        Uses the three-dot form, so the diff is against the merge base rather
        than against whatever the base branch has done since. A reviewer
        looking at an attempt should see what the attempt changed, not what the
        rest of the team changed while they were reading.

        Args:
            branch: The branch to diff.
            base: What to diff against. Defaults to the repository's HEAD.
            context: Lines of context per hunk.

        Returns:
            The patch, empty when the branch changed nothing.

        Raises:
            GitCommandError: If the branch does not exist.
        """
        target = base or "HEAD"
        result = await self.run(
            "diff",
            f"--unified={max(0, context)}",
            "--no-color",
            f"{target}...{branch}",
        )
        return result.out

    async def is_repository(self) -> bool:
        """Whether the path is inside a Git working tree."""
        result = await self.run(
            "rev-parse", "--is-inside-work-tree", check=False
        )
        return result.ok and result.out == "true"

    async def toplevel(self) -> Path:
        """Return the repository root as Git reports it.

        Raises:
            NotARepositoryError: If the path is not in a repository.
        """
        result = await self.run("rev-parse", "--show-toplevel", check=False)
        if not result.ok:
            raise NotARepositoryError(
                f"{self._path} is not inside a Git repository",
                detail={"path": str(self._path)},
            )
        return Path(result.out)

    async def status_porcelain(self, cwd: Path | None = None) -> tuple[str, ...]:
        """Return porcelain status lines for the working tree."""
        return (await self.run("status", "--porcelain", cwd=cwd)).lines()

    async def is_clean(self, cwd: Path | None = None) -> bool:
        """Whether the working tree has no uncommitted changes."""
        return not await self.status_porcelain(cwd=cwd)

    async def require_clean(self, cwd: Path | None = None) -> None:
        """Raise unless the working tree is clean (FR-3.7).

        Raises:
            DirtyRepositoryError: Listing the dirty paths.
        """
        dirty = await self.status_porcelain(cwd=cwd)
        if dirty:
            raise DirtyRepositoryError(
                f"{cwd or self._path} has uncommitted changes; commit or stash "
                "them before starting a run",
                detail={"paths": list(dirty)},
            )

    async def current_branch(self, cwd: Path | None = None) -> str:
        """Return the checked-out branch name, or ``""`` when detached."""
        result = await self.run("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
        name = result.out
        return "" if name == "HEAD" else name

    async def head_sha(self, ref: str = "HEAD") -> str:
        """Return the commit SHA a ref points at."""
        return (await self.run("rev-parse", ref)).out

    async def ref_exists(self, ref: str) -> bool:
        """Whether a ref resolves."""
        result = await self.run(
            "rev-parse", "--verify", "--quiet", ref, check=False
        )
        return result.ok

    async def default_branch(self) -> str:
        """Best-effort guess at the repository's default branch.

        Tries the remote's published head, then the conventional names. The
        answer matters because merging into it is forbidden (FR-3.6), so when
        it cannot be determined the caller should treat every candidate as
        protected rather than assume none is.
        """
        result = await self.run(
            "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False
        )
        if result.ok and result.out:
            return result.out.removeprefix("origin/")
        for candidate in ("main", "master"):
            if await self.ref_exists(f"refs/heads/{candidate}"):
                return candidate
        return await self.current_branch()
