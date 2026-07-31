"""Merging attempt branches, and detecting conflicts before they happen.

Merges here never touch a working tree. They are computed with Git's plumbing —
``merge-tree`` produces the merged tree, ``commit-tree`` turns it into a commit,
``update-ref`` moves the branch — so there is no index to leave half-merged and
no checkout to disturb. That satisfies two requirements at once, and satisfies
them *structurally* rather than by remembering to clean up:

* **The operator's working tree is never touched** (FR-3.2). Nothing is checked
  out, so nothing can be.
* **A conflict leaves no half-merged index** (FR-3.5). The conflicting merge is
  never applied anywhere; it is computed, found to conflict, and reported.

:meth:`MergeManager.detect_conflicts` runs the same computation and throws the
result away, so a caller can ask "would this conflict?" without side effects.

**Merging into the default branch is refused.** OrchestratorPro produces an
integration branch and stops; the decision to merge that into ``main`` is the
operator's, made with a normal review, outside this tool (FR-3.6).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from orchestrator.core.events import OrchestratorError, RunId
from orchestrator.git_manager.branch import BranchManager, BranchNamer
from orchestrator.git_manager.repo import GitRepository

__all__ = [
    "MergeError",
    "MergeManager",
    "MergeResult",
    "MergeStatus",
    "ProtectedBranchError",
]

_AUTHOR_NAME: Final = "OrchestratorPro"
_AUTHOR_EMAIL: Final = "orchestratorpro@localhost"


class MergeError(OrchestratorError):
    """A merge could not be computed or applied."""

    code = "merge"
    retryable = False


class ProtectedBranchError(MergeError):
    """A merge into a protected branch was refused."""

    code = "protected_branch"
    retryable = False


class MergeStatus(StrEnum):
    """How a merge attempt resolved."""

    MERGED = "merged"
    ALREADY_MERGED = "already_merged"
    CONFLICT = "conflict"
    NOTHING_TO_MERGE = "nothing_to_merge"


@dataclass(frozen=True, slots=True)
class MergeResult:
    """The outcome of one merge."""

    status: MergeStatus
    source: str
    target: str
    sha: str | None = None
    conflicted_paths: tuple[str, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether the source's work is now contained in the target."""
        return self.status in (MergeStatus.MERGED, MergeStatus.ALREADY_MERGED)

    @property
    def conflicted(self) -> bool:
        """Whether the merge was refused because of conflicts."""
        return self.status is MergeStatus.CONFLICT


class MergeManager:
    """Computes and applies merges without checking anything out."""

    __slots__ = ("_branches", "_protected", "_repo")

    def __init__(
        self,
        repo: GitRepository,
        *,
        branches: BranchManager | None = None,
        protected: Iterable[str] = (),
    ) -> None:
        """Bind the manager to a repository.

        Args:
            repo: The repository to merge in.
            branches: Branch manager. One is created if omitted.
            protected: Extra branch names that may never be merged into. The
                repository's default branch is always protected, whether or not
                it is listed here.
        """
        self._repo = repo
        self._branches = branches or BranchManager(repo)
        self._protected = frozenset(protected)

    @property
    def repo(self) -> GitRepository:
        """The underlying repository."""
        return self._repo

    async def protected_branches(self) -> frozenset[str]:
        """Return every branch this manager refuses to merge into."""
        default = await self._repo.default_branch()
        return self._protected | ({default} if default else frozenset())

    async def _assert_target_allowed(self, target: str) -> None:
        """Raise if ``target`` must not receive merges.

        Raises:
            ProtectedBranchError: If the target is protected.
        """
        if target in await self.protected_branches():
            raise ProtectedBranchError(
                f"refusing to merge into {target!r}: OrchestratorPro produces an "
                "integration branch and stops — merging to the default branch is "
                "the operator's decision",
                detail={"target": target},
            )

    # ------------------------------------------------------------- inspection

    async def has_merged(self, source: str, target: str) -> bool:
        """Whether ``source`` is already contained in ``target``.

        The idempotency check recovery depends on: after a crash between the
        merge and the event that recorded it, the answer is asked of Git rather
        than assumed (``docs/020_ARCHITECTURE`` §5.1).
        """
        result = await self._repo.run(
            "merge-base", "--is-ancestor", source, target, check=False
        )
        return result.ok

    async def detect_conflicts(self, source: str, target: str) -> tuple[str, ...]:
        """Return the paths that would conflict, without changing anything.

        Args:
            source: The branch to merge from.
            target: The branch to merge into.

        Returns:
            The conflicted paths, empty when the merge would apply cleanly.

        Raises:
            MergeError: If either ref does not resolve.
        """
        await self._require_refs(source, target)
        _tree, conflicts = await self._merge_tree(source, target)
        return conflicts

    async def _require_refs(self, *refs: str) -> None:
        """Raise unless every ref resolves.

        Raises:
            MergeError: Naming the refs that are missing.
        """
        missing = [ref for ref in refs if not await self._repo.ref_exists(ref)]
        if missing:
            raise MergeError(
                f"cannot merge: unknown ref(s) {', '.join(missing)}",
                detail={"missing": missing},
            )

    async def _merge_tree(self, source: str, target: str) -> tuple[str, tuple[str, ...]]:
        """Compute the merged tree, reporting conflicts instead of applying them.

        Returns:
            The merged tree's object id, and the conflicted paths. A non-empty
            conflict list means the tree is not usable.
        """
        result = await self._repo.run(
            "merge-tree", "--write-tree", "--name-only", target, source, check=False
        )
        if result.returncode not in (0, 1):
            raise MergeError(
                f"could not compute a merge of {source} into {target}: "
                f"{result.stderr.strip()}",
                detail={
                    "source": source,
                    "target": target,
                    "returncode": result.returncode,
                },
            )

        lines = result.stdout.split("\n")
        tree = lines[0].strip() if lines else ""
        conflicts: list[str] = []
        for line in lines[1:]:
            if not line.strip():
                break
            conflicts.append(line.strip())

        if result.returncode == 0:
            return tree, ()
        return tree, tuple(sorted(conflicts))

    # ---------------------------------------------------------------- merging

    async def ensure_integration_branch(
        self, run_id: RunId | str, *, start_point: str = "HEAD"
    ) -> str:
        """Create the run's integration branch if it is missing.

        Returns:
            The integration branch name.
        """
        name = BranchNamer.integration(run_id)
        await self._branches.ensure(name, start_point=start_point)
        return name

    async def merge(
        self, source: str, target: str, *, message: str | None = None
    ) -> MergeResult:
        """Merge ``source`` into ``target`` without checking anything out.

        Args:
            source: The branch whose work should be integrated.
            target: The branch to integrate into. Must not be protected.
            message: The merge commit's message. One is generated if omitted.

        Returns:
            The result. A conflict is reported, not raised — the caller decides
            whether a conflicting task is a failure or a retry.

        Raises:
            ProtectedBranchError: If the target is protected (FR-3.6).
            MergeError: If a ref is unknown, or Git could not apply the result.
        """
        await self._assert_target_allowed(target)
        await self._require_refs(source, target)

        source_sha = await self._repo.head_sha(source)
        target_sha = await self._repo.head_sha(target)

        if source_sha == target_sha:
            return MergeResult(
                status=MergeStatus.NOTHING_TO_MERGE,
                source=source,
                target=target,
                sha=target_sha,
                detail={"reason": "the branches point at the same commit"},
            )
        if await self.has_merged(source, target):
            return MergeResult(
                status=MergeStatus.ALREADY_MERGED,
                source=source,
                target=target,
                sha=target_sha,
            )

        tree, conflicts = await self._merge_tree(source, target)
        if conflicts:
            return MergeResult(
                status=MergeStatus.CONFLICT,
                source=source,
                target=target,
                conflicted_paths=conflicts,
                detail={"reason": "the merge would conflict; nothing was applied"},
            )

        subject = message or f"Merge {source} into {target}"
        commit = await self._repo.run(
            "-c",
            f"user.name={_AUTHOR_NAME}",
            "-c",
            f"user.email={_AUTHOR_EMAIL}",
            "commit-tree",
            tree,
            "-p",
            target_sha,
            "-p",
            source_sha,
            "-m",
            subject,
            check=False,
        )
        if not commit.ok:
            raise MergeError(
                f"could not create the merge commit for {source} into {target}: "
                f"{commit.stderr.strip()}",
                detail={"source": source, "target": target},
            )
        new_sha = commit.out

        # Compare-and-swap: passing the expected old value means a concurrent
        # writer cannot be silently clobbered.
        update = await self._repo.run(
            "update-ref", f"refs/heads/{target}", new_sha, target_sha, check=False
        )
        if not update.ok:
            raise MergeError(
                f"could not advance {target} to {new_sha}: {update.stderr.strip()}; "
                "the branch moved while the merge was being computed",
                detail={"target": target, "expected": target_sha, "new": new_sha},
            )

        return MergeResult(
            status=MergeStatus.MERGED,
            source=source,
            target=target,
            sha=new_sha,
            detail={"parents": [target_sha, source_sha]},
        )

    async def merge_all(
        self, sources: Sequence[str], target: str, *, stop_on_conflict: bool = False
    ) -> tuple[MergeResult, ...]:
        """Merge several branches into one target, in order.

        Merges are serialized by construction — one call at a time, each
        computed against the target's current tip — which is what keeps
        concurrent attempts from producing a conflicted result nobody asked for
        (``docs/020_ARCHITECTURE`` §4).

        Args:
            sources: Branches to integrate, in order.
            target: The branch to integrate into.
            stop_on_conflict: Stop at the first conflict instead of continuing.

        Returns:
            One result per source attempted.
        """
        results: list[MergeResult] = []
        for source in sources:
            result = await self.merge(source, target)
            results.append(result)
            if result.conflicted and stop_on_conflict:
                break
        return tuple(results)

    async def integrate_attempt(
        self,
        *,
        run_id: RunId | str,
        source: str,
        start_point: str = "HEAD",
        message: str | None = None,
    ) -> MergeResult:
        """Merge one attempt branch into its run's integration branch.

        The common path: ensures the integration branch exists, then merges.

        Returns:
            The merge result.
        """
        target = await self.ensure_integration_branch(run_id, start_point=start_point)
        return await self.merge(source, target, message=message)
