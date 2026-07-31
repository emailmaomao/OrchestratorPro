"""Worktree isolation — one working tree per attempt.

``ORCHESTRATOR_PRO_SPEC`` §4.1 admits no exception here: every attempt gets its
own ``git worktree`` on its own branch, and no two attempts ever share a working
tree (FR-3.1). There is no "fast path" that skips this, because that path is
where correctness goes to die — two agents editing one tree produce a diff
neither of them wrote.

The second guarantee is quieter but matters as much: the operator's checked-out
tree is never touched (FR-3.2). Worktrees are created *outside* the repository
by default, so a run cannot litter the working copy the human is using, and
every destructive path is checked against the repository root before it is
removed.

A failed attempt's worktree is **retained** by default. The point of an attempt
that failed is to be looked at.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.events import AttemptId, OrchestratorError, RunId, TaskId
from orchestrator.git_manager.branch import BranchManager, BranchNamer
from orchestrator.git_manager.repo import GitRepository

__all__ = ["Workspace", "WorkspaceError", "WorkspaceManager"]

#: Where worktrees live when the caller does not say. A sibling of the
#: repository, so the operator's working copy stays uncluttered.
_DEFAULT_ROOT_NAME = ".orchestratorpro-worktrees"


class WorkspaceError(OrchestratorError):
    """A worktree could not be created, found, or removed."""

    code = "workspace"
    retryable = False


@dataclass(frozen=True, slots=True)
class Workspace:
    """One attempt's isolated working tree."""

    path: Path
    branch: str
    run_id: str
    task_id: str
    attempt: int
    attempt_id: str | None = None

    @property
    def name(self) -> str:
        """The worktree directory's name."""
        return self.path.name

    def __str__(self) -> str:
        return f"{self.branch} at {self.path}"


class WorkspaceManager:
    """Creates and destroys per-attempt worktrees."""

    __slots__ = ("_branches", "_repo", "_root")

    def __init__(
        self,
        repo: GitRepository,
        *,
        root: Path | None = None,
        branches: BranchManager | None = None,
    ) -> None:
        """Bind the manager to a repository.

        Args:
            repo: The repository to create worktrees from.
            root: Directory to hold worktrees. Defaults to a sibling of the
                repository, keeping the operator's checkout clean.
            branches: Branch manager. One is created if omitted.
        """
        self._repo = repo
        self._root = Path(root) if root is not None else repo.path.parent / _DEFAULT_ROOT_NAME
        self._branches = branches or BranchManager(repo)

    @property
    def root(self) -> Path:
        """Where worktrees are created."""
        return self._root

    @property
    def repo(self) -> GitRepository:
        """The underlying repository."""
        return self._repo

    def path_for(self, run_id: RunId | str, task_id: TaskId | str, attempt: int) -> Path:
        """Return the directory one attempt's worktree would occupy."""
        return self._root / str(run_id) / f"{task_id}-{attempt}"

    def _assert_removable(self, path: Path) -> None:
        """Refuse to remove anything that is not a worktree we own.

        Raises:
            WorkspaceError: If the path is the repository itself, or outside the
                configured worktree root.
        """
        resolved = path.resolve()
        repo_root = self._repo.path.resolve()
        if resolved == repo_root:
            raise WorkspaceError(
                "refusing to remove the repository's own working tree",
                detail={"path": str(resolved)},
            )
        if not resolved.is_relative_to(self._root.resolve()):
            raise WorkspaceError(
                f"refusing to remove {resolved}: it is outside the worktree root "
                f"{self._root.resolve()}",
                detail={"path": str(resolved), "root": str(self._root)},
            )

    async def create(
        self,
        *,
        run_id: RunId | str,
        task_id: TaskId | str,
        attempt: int,
        start_point: str = "HEAD",
        attempt_id: AttemptId | None = None,
    ) -> Workspace:
        """Create an isolated worktree on a fresh branch for one attempt.

        Args:
            run_id: The run this attempt belongs to.
            task_id: The task being attempted.
            attempt: The 1-based attempt number.
            start_point: The commit the branch starts from.
            attempt_id: Recorded on the workspace for correlation.

        Returns:
            The created workspace.

        Raises:
            WorkspaceError: If the target directory is already occupied.
        """
        branch = BranchNamer.build(run_id, task_id, attempt)
        path = self.path_for(run_id, task_id, attempt)

        if path.exists() and any(path.iterdir()):
            raise WorkspaceError(
                f"cannot create a worktree at {path}: the directory is not empty",
                detail={"path": str(path), "branch": branch},
            )

        path.parent.mkdir(parents=True, exist_ok=True)

        if await self._branches.exists(branch):
            # Recovery: the branch survived a crash, so reuse it rather than
            # failing. Creating a worktree is idempotent from the caller's view.
            await self._repo.run("worktree", "add", str(path), branch)
        else:
            await self._repo.run("worktree", "add", "-b", branch, str(path), start_point)

        return Workspace(
            path=path,
            branch=branch,
            run_id=str(run_id),
            task_id=str(task_id),
            attempt=attempt,
            attempt_id=str(attempt_id) if attempt_id is not None else None,
        )

    async def destroy(
        self, workspace: Workspace, *, delete_branch: bool = False, force: bool = False
    ) -> None:
        """Remove a worktree, and optionally its branch.

        Args:
            workspace: The workspace to remove.
            delete_branch: Also delete the attempt branch. Off by default: the
                commits are usually the only record of what an attempt did.
            force: Remove even with uncommitted changes, and force the branch
                deletion.

        Raises:
            WorkspaceError: If the path is not a worktree this manager owns.
        """
        self._assert_removable(workspace.path)

        args = ["worktree", "remove", str(workspace.path)]
        if force:
            args.append("--force")
        result = await self._repo.run(*args, check=False, authorize=True)

        if not result.ok and workspace.path.exists():
            # Git refuses when the directory was already gone or is dirty. Fall
            # back to removing the directory and pruning the administrative
            # record, so a half-removed worktree cannot wedge the next attempt.
            if force:
                shutil.rmtree(workspace.path, ignore_errors=True)
                await self.prune()
            else:
                raise WorkspaceError(
                    f"could not remove worktree at {workspace.path}: "
                    f"{result.stderr.strip()}",
                    detail={"path": str(workspace.path), "stderr": result.stderr},
                )

        if delete_branch:
            await self._branches.delete(workspace.branch, force=force)

    async def prune(self) -> None:
        """Discard administrative records of worktrees whose directories are gone."""
        await self._repo.run("worktree", "prune", authorize=True)

    async def list(self) -> tuple[Path, ...]:
        """Return every worktree path Git knows about, excluding the main one."""
        result = await self._repo.run("worktree", "list", "--porcelain")
        paths: list[Path] = []
        for line in result.lines():
            if line.startswith("worktree "):
                paths.append(Path(line.removeprefix("worktree ").strip()))
        main = self._repo.path.resolve()
        return tuple(p for p in paths if p.resolve() != main)

    async def list_managed(self) -> tuple[Path, ...]:
        """Return only the worktrees inside this manager's root."""
        root = self._root.resolve()
        return tuple(
            path
            for path in await self.list()
            if path.resolve().is_relative_to(root)
        )

    async def cleanup_run(
        self, run_id: RunId | str, *, force: bool = True
    ) -> tuple[Path, ...]:
        """Remove every worktree belonging to one run.

        Branches are left alone: they hold the commits, which are the record of
        what happened.

        Returns:
            The paths that were removed.
        """
        run_root = (self._root / str(run_id)).resolve()
        removed: list[Path] = []
        for path in await self.list():
            if not path.resolve().is_relative_to(run_root):
                continue
            self._assert_removable(path)
            args = ["worktree", "remove", str(path)]
            if force:
                args.append("--force")
            await self._repo.run(*args, check=False, authorize=True)
            removed.append(path)
        await self.prune()
        return tuple(removed)

    async def is_isolated(self, workspaces: Iterable[Workspace]) -> bool:
        """Whether every workspace occupies a distinct path and branch.

        The invariant FR-3.1 rests on. Cheap enough to assert in a run's
        pre-flight rather than trusting it.
        """
        collected = list(workspaces)
        paths = {ws.path.resolve() for ws in collected}
        branches = {ws.branch for ws in collected}
        return len(paths) == len(collected) and len(branches) == len(collected)
