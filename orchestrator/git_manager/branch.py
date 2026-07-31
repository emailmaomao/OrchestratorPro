"""Branch naming and lifecycle.

Branches OrchestratorPro creates follow one shape (FR-3.3)::

    orchestrator/<run-id>/<task-id>/<attempt>

That convention is load-bearing rather than cosmetic. It is how the system knows
which branches are **its own**, and therefore which it may delete. A branch that
does not parse as one of ours is refused for every destructive operation, no
matter what the caller asks (FR-3.4) — an operator's feature branch that happens
to be passed in by mistake must survive the mistake.

Deleting a branch with unmerged commits is refused as well, unless the caller
explicitly asks for it. Losing an attempt's work because the bookkeeping said it
was finished is exactly the failure this guard exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from orchestrator.core.events import AttemptId, OrchestratorError, RunId, TaskId
from orchestrator.git_manager.repo import GitRepository

__all__ = [
    "BRANCH_PREFIX",
    "BranchManager",
    "BranchNamer",
    "BranchOwnershipError",
    "BranchRef",
    "UnmergedBranchError",
]

#: Every branch this system creates begins with this segment.
BRANCH_PREFIX: Final = "orchestrator"

#: ``orchestrator/<run>/<task>/<attempt>``. Identifier characters only, so a
#: crafted name cannot smuggle a path traversal or a Git refspec option.
_BRANCH_PATTERN: Final = re.compile(
    rf"^{BRANCH_PREFIX}/(?P<run>[A-Za-z0-9_]+)/(?P<task>[A-Za-z0-9_]+)/(?P<attempt>\d+)$"
)


class BranchOwnershipError(OrchestratorError):
    """A destructive operation was attempted on a branch we did not create."""

    code = "branch_not_owned"
    retryable = False


class UnmergedBranchError(OrchestratorError):
    """A branch with unmerged commits was almost deleted."""

    code = "branch_unmerged"
    retryable = False


@dataclass(frozen=True, slots=True)
class BranchRef:
    """A parsed OrchestratorPro branch name."""

    name: str
    run_id: str
    task_id: str
    attempt: int


class BranchNamer:
    """Builds and recognizes OrchestratorPro branch names."""

    __slots__ = ()

    @staticmethod
    def build(run_id: RunId | str, task_id: TaskId | str, attempt: int) -> str:
        """Return the branch name for one attempt.

        Args:
            run_id: The run.
            task_id: The task.
            attempt: The 1-based attempt number.

        Returns:
            The branch name.

        Raises:
            BranchOwnershipError: If ``attempt`` is not positive, or an
                identifier contains characters that do not belong in a ref.
        """
        if attempt < 1:
            raise BranchOwnershipError(
                f"attempt numbers are 1-based, got {attempt}",
                detail={"attempt": attempt},
            )
        name = f"{BRANCH_PREFIX}/{run_id}/{task_id}/{attempt}"
        if not _BRANCH_PATTERN.match(name):
            raise BranchOwnershipError(
                f"identifiers produced an invalid branch name: {name!r}",
                detail={"name": name},
            )
        return name

    @staticmethod
    def parse(name: str) -> BranchRef | None:
        """Parse a branch name, or return ``None`` if it is not ours."""
        match = _BRANCH_PATTERN.match(name)
        if match is None:
            return None
        return BranchRef(
            name=name,
            run_id=match.group("run"),
            task_id=match.group("task"),
            attempt=int(match.group("attempt")),
        )

    @staticmethod
    def is_owned(name: str) -> bool:
        """Whether a branch name is one this system created."""
        return _BRANCH_PATTERN.match(name) is not None

    @staticmethod
    def run_prefix(run_id: RunId | str) -> str:
        """Return the prefix matching every branch belonging to one run."""
        return f"{BRANCH_PREFIX}/{run_id}/"

    @staticmethod
    def integration(run_id: RunId | str) -> str:
        """Return the integration branch name for a run.

        Deliberately *not* matched by :meth:`is_owned`: the integration branch
        is what the operator reviews and merges, so it must not be deletable by
        the same automatic bookkeeping that cleans up attempt branches.
        """
        return f"{BRANCH_PREFIX}/{run_id}/integration"


class BranchManager:
    """Creates, inspects, and safely deletes branches."""

    __slots__ = ("_repo",)

    def __init__(self, repo: GitRepository) -> None:
        """Bind the manager to a repository."""
        self._repo = repo

    @property
    def repo(self) -> GitRepository:
        """The underlying repository."""
        return self._repo

    async def exists(self, name: str) -> bool:
        """Whether a local branch exists."""
        return await self._repo.ref_exists(f"refs/heads/{name}")

    async def create(self, name: str, *, start_point: str = "HEAD") -> str:
        """Create a branch without checking it out.

        Args:
            name: The branch to create.
            start_point: The commit or ref to branch from.

        Returns:
            The branch name.

        Raises:
            BranchOwnershipError: If the branch already exists.
        """
        if await self.exists(name):
            raise BranchOwnershipError(
                f"branch {name!r} already exists", detail={"branch": name}
            )
        await self._repo.run("branch", name, start_point)
        return name

    async def ensure(self, name: str, *, start_point: str = "HEAD") -> str:
        """Create a branch if it is missing. Idempotent, for crash recovery."""
        if not await self.exists(name):
            await self._repo.run("branch", name, start_point)
        return name

    async def list(self, prefix: str | None = None) -> tuple[str, ...]:
        """Return local branch names, optionally filtered by prefix."""
        result = await self._repo.run(
            "for-each-ref", "--format=%(refname:short)", "refs/heads"
        )
        names = result.lines()
        if prefix is not None:
            names = tuple(name for name in names if name.startswith(prefix))
        return tuple(sorted(names))

    async def list_owned(self) -> tuple[str, ...]:
        """Return only the branches this system created."""
        return tuple(name for name in await self.list() if BranchNamer.is_owned(name))

    async def is_merged_into(self, name: str, target: str) -> bool:
        """Whether every commit on ``name`` is already contained in ``target``."""
        result = await self._repo.run(
            "merge-base", "--is-ancestor", name, target, check=False
        )
        return result.ok

    async def delete(self, name: str, *, force: bool = False) -> bool:
        """Delete a branch, refusing to lose work or touch what is not ours.

        Args:
            name: The branch to delete.
            force: Delete even if it has unmerged commits. Ownership is still
                required — this flag does not override that.

        Returns:
            ``True`` if the branch was deleted, ``False`` if it did not exist.

        Raises:
            BranchOwnershipError: If the branch is not one we created (FR-3.4).
            UnmergedBranchError: If it has unmerged commits and ``force`` is not
                set.
        """
        if not BranchNamer.is_owned(name):
            raise BranchOwnershipError(
                f"refusing to delete {name!r}: OrchestratorPro did not create it, "
                "and only deletes branches matching "
                f"{BRANCH_PREFIX}/<run>/<task>/<attempt>",
                detail={"branch": name},
            )
        if not await self.exists(name):
            return False

        if not force:
            # -d refuses an unmerged branch; translate Git's refusal into ours
            # rather than reporting a raw command failure.
            result = await self._repo.run(
                "branch", "-d", name, check=False, authorize=True
            )
            if not result.ok:
                raise UnmergedBranchError(
                    f"branch {name!r} has commits that are not merged anywhere; "
                    "pass force=True only if that work is genuinely disposable",
                    detail={"branch": name, "stderr": result.stderr.strip()},
                )
            return True

        await self._repo.run("branch", "-D", name, authorize=True)
        return True

    async def delete_run_branches(
        self, run_id: RunId | str, *, force: bool = False
    ) -> tuple[str, ...]:
        """Delete every attempt branch belonging to one run.

        The run's integration branch is deliberately left behind: it is the
        deliverable the operator reviews.

        Returns:
            The branches that were deleted.
        """
        prefix = BranchNamer.run_prefix(run_id)
        deleted: list[str] = []
        for name in await self.list(prefix=prefix):
            if BranchNamer.is_owned(name) and await self.delete(name, force=force):
                deleted.append(name)
        return tuple(deleted)

    async def current(self) -> str:
        """Return the branch currently checked out in the main working tree."""
        return await self._repo.current_branch()

    async def branch_for(
        self, run_id: RunId, task_id: TaskId, attempt: int, *, start_point: str = "HEAD"
    ) -> str:
        """Create the branch for one attempt and return its name."""
        return await self.ensure(
            BranchNamer.build(run_id, task_id, attempt), start_point=start_point
        )

    @staticmethod
    def attempt_id_hint(attempt_id: AttemptId) -> str:
        """Return a short, human-readable tag for an attempt identifier.

        Branch names carry the task and attempt number rather than the attempt
        identifier, which would make them unreadably long. This is for commit
        trailers and logs, where the full identifier is what correlates.
        """
        return str(attempt_id)
