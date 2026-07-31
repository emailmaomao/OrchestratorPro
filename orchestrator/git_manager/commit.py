"""Staging, committing, and inspecting what an attempt changed.

All operations run **inside an attempt's worktree**, never in the operator's
checkout. The manager takes a :class:`~orchestrator.git_manager.workspace.Workspace`
rather than a bare path so that a call site cannot accidentally aim a commit at
the main working tree.

Committing nothing is not an error. An attempt that concluded no change was
needed is a legitimate outcome, and turning it into a failure would push agents
toward inventing changes to look productive. :meth:`CommitManager.commit`
reports ``created=False`` and leaves the branch alone.

Identity is set per-invocation rather than written into the repository's config.
Mutating a user's Git identity as a side effect of running a tool would be
rude, and it would persist long after the run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from orchestrator.core.events import OrchestratorError
from orchestrator.git_manager.repo import GitRepository
from orchestrator.git_manager.workspace import Workspace

__all__ = ["CommitError", "CommitManager", "CommitResult", "FileChange"]

_DEFAULT_AUTHOR_NAME: Final = "OrchestratorPro"
_DEFAULT_AUTHOR_EMAIL: Final = "orchestratorpro@localhost"

#: Porcelain status codes that mean the path is untracked or ignored.
_UNTRACKED: Final = frozenset({"??", "!!"})


class CommitError(OrchestratorError):
    """A commit could not be created."""

    code = "commit"
    retryable = False


@dataclass(frozen=True, slots=True)
class FileChange:
    """One changed path, with the status Git reported for it."""

    path: str
    status: str

    @property
    def is_untracked(self) -> bool:
        """Whether Git has never seen this path before."""
        return self.status in _UNTRACKED

    @property
    def is_deleted(self) -> bool:
        """Whether the path was removed."""
        return "D" in self.status


@dataclass(frozen=True, slots=True)
class CommitResult:
    """The outcome of an attempted commit."""

    created: bool
    sha: str | None = None
    files: tuple[str, ...] = ()
    message: str = ""
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """Whether there was nothing to commit."""
        return not self.created


class CommitManager:
    """Stages and commits work inside an attempt's worktree."""

    __slots__ = ("_author_email", "_author_name", "_repo")

    def __init__(
        self,
        repo: GitRepository,
        *,
        author_name: str = _DEFAULT_AUTHOR_NAME,
        author_email: str = _DEFAULT_AUTHOR_EMAIL,
    ) -> None:
        """Bind the manager to a repository.

        Args:
            repo: The repository the worktrees belong to.
            author_name: Name recorded on commits this system creates.
            author_email: Email recorded on commits this system creates.
        """
        self._repo = repo
        self._author_name = author_name
        self._author_email = author_email

    @property
    def repo(self) -> GitRepository:
        """The underlying repository."""
        return self._repo

    # ------------------------------------------------------------ inspection

    async def changes(self, workspace: Workspace) -> tuple[FileChange, ...]:
        """Return every change in the worktree, staged or not."""
        lines = await self._repo.status_porcelain(cwd=workspace.path)
        changes: list[FileChange] = []
        for line in lines:
            # Porcelain v1 is a fixed layout: two status columns, a space, then
            # the path. Splitting on the first space is wrong whenever the index
            # column is blank, which is the common " M path" case.
            if len(line) < 4:
                continue
            status = line[:2].strip()
            cleaned = line[3:].strip().strip('"')
            if " -> " in cleaned:  # a rename reports "old -> new"
                cleaned = cleaned.split(" -> ", 1)[1]
            if cleaned:
                changes.append(FileChange(path=cleaned, status=status))
        return tuple(changes)

    async def changed_files(self, workspace: Workspace) -> tuple[str, ...]:
        """Return the paths that differ from HEAD, in sorted order."""
        return tuple(sorted(change.path for change in await self.changes(workspace)))

    async def has_changes(self, workspace: Workspace) -> bool:
        """Whether the worktree differs from HEAD."""
        return bool(await self.changes(workspace))

    async def diff(self, workspace: Workspace, base: str = "HEAD") -> str:
        """Return the unified diff between ``base`` and the worktree."""
        result = await self._repo.run("diff", base, cwd=workspace.path)
        return result.stdout

    async def diff_names(self, workspace: Workspace, base: str) -> tuple[str, ...]:
        """Return the paths that differ between ``base`` and the branch tip."""
        result = await self._repo.run(
            "diff", "--name-only", f"{base}...{workspace.branch}", cwd=workspace.path
        )
        return tuple(sorted(result.lines()))

    async def log(self, workspace: Workspace, *, limit: int = 20) -> tuple[str, ...]:
        """Return recent commit subjects on the workspace's branch."""
        result = await self._repo.run(
            "log", f"--max-count={limit}", "--format=%H %s", cwd=workspace.path
        )
        return result.lines()

    # -------------------------------------------------------------- mutation

    async def stage_all(self, workspace: Workspace) -> tuple[str, ...]:
        """Stage every change in the worktree, including deletions.

        Returns:
            The staged paths.
        """
        await self._repo.run("add", "--all", cwd=workspace.path)
        return await self.staged_files(workspace)

    async def stage(self, workspace: Workspace, paths: Sequence[str]) -> tuple[str, ...]:
        """Stage specific paths.

        Args:
            workspace: The worktree to operate in.
            paths: Repository-relative paths.

        Returns:
            The staged paths.

        Raises:
            CommitError: If no paths were given — an empty stage is almost
                always a caller bug rather than an intent.
        """
        if not paths:
            raise CommitError(
                "stage() needs at least one path; use stage_all() to stage everything"
            )
        await self._repo.run("add", "--", *paths, cwd=workspace.path)
        return await self.staged_files(workspace)

    async def staged_files(self, workspace: Workspace) -> tuple[str, ...]:
        """Return the paths currently staged."""
        result = await self._repo.run(
            "diff", "--cached", "--name-only", cwd=workspace.path
        )
        return tuple(sorted(result.lines()))

    async def commit(
        self,
        workspace: Workspace,
        message: str,
        *,
        stage: bool = True,
        trailers: Mapping[str, str] | None = None,
    ) -> CommitResult:
        """Commit the worktree's changes.

        Args:
            workspace: The worktree to commit in.
            message: The commit subject and body.
            stage: Stage everything first. Set ``False`` when the caller has
                already staged a subset deliberately.
            trailers: Key-value trailers appended to the message, for
                correlating a commit back to its run and attempt.

        Returns:
            The result. ``created=False`` when there was nothing to commit,
            which is a normal outcome rather than a failure.

        Raises:
            CommitError: If the message is blank, or Git refused the commit for
                a reason other than an empty tree.
        """
        if not message.strip():
            raise CommitError("a commit message must not be blank")

        if stage:
            await self.stage_all(workspace)

        staged = await self.staged_files(workspace)
        if not staged:
            return CommitResult(
                created=False,
                files=(),
                message=message,
                detail={"reason": "nothing to commit"},
            )

        full_message = self._with_trailers(message, workspace, trailers)
        result = await self._repo.run(
            "-c",
            f"user.name={self._author_name}",
            "-c",
            f"user.email={self._author_email}",
            "commit",
            "--message",
            full_message,
            cwd=workspace.path,
            check=False,
        )
        if not result.ok:
            raise CommitError(
                f"could not commit in {workspace.path}: {result.stderr.strip()}",
                detail={
                    "branch": workspace.branch,
                    "stderr": result.stderr,
                    "staged": list(staged),
                },
            )

        sha = (await self._repo.run("rev-parse", "HEAD", cwd=workspace.path)).out
        return CommitResult(created=True, sha=sha, files=staged, message=full_message)

    def _with_trailers(
        self,
        message: str,
        workspace: Workspace,
        trailers: Mapping[str, str] | None,
    ) -> str:
        """Append correlation trailers to a commit message."""
        collected: dict[str, str] = {
            "Orchestrator-Run": workspace.run_id,
            "Orchestrator-Task": workspace.task_id,
            "Orchestrator-Attempt": str(workspace.attempt),
        }
        if workspace.attempt_id:
            collected["Orchestrator-Attempt-Id"] = workspace.attempt_id
        collected.update(trailers or {})

        rendered = "\n".join(f"{key}: {value}" for key, value in collected.items())
        return f"{message.rstrip()}\n\n{rendered}\n"

    async def commit_if_changed(
        self, workspace: Workspace, message: str, **kwargs: object
    ) -> CommitResult:
        """Commit only when the worktree differs from HEAD.

        Convenience for the common attempt-finished path, where "no changes" is
        expected often enough that checking first avoids a pointless stage.
        """
        if not await self.has_changes(workspace):
            return CommitResult(
                created=False, message=message, detail={"reason": "no changes"}
            )
        return await self.commit(workspace, message, **kwargs)  # type: ignore[arg-type]
