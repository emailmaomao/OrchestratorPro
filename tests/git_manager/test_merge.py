"""Tests for merging and conflict detection."""

from __future__ import annotations

import pytest

from orchestrator.core.events import RunId, TaskId
from orchestrator.git_manager.branch import BranchManager, BranchNamer
from orchestrator.git_manager.commit import CommitManager
from orchestrator.git_manager.merge import (
    MergeError,
    MergeManager,
    MergeStatus,
    ProtectedBranchError,
)
from orchestrator.git_manager.repo import GitRepository
from orchestrator.git_manager.workspace import WorkspaceManager

from tests.git_manager.conftest import FakeGitRunner, TempRepo, requires_git, run


class TestProtection:
    """FR-3.6: the default branch is never merged into."""

    def test_merging_into_the_default_branch_is_refused(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/main\n"
        )
        manager = MergeManager(fake_repo)
        with pytest.raises(ProtectedBranchError, match="operator's decision"):
            run(manager.merge("orchestrator/r/t/1", "main"))

    def test_no_git_command_runs_for_a_refused_merge(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/main\n"
        )
        with pytest.raises(ProtectedBranchError):
            run(MergeManager(fake_repo).merge("x", "main"))
        assert not fake_runner.ran("merge-tree")
        assert not fake_runner.ran("update-ref")

    def test_extra_protected_branches_are_honoured(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/main\n"
        )
        manager = MergeManager(fake_repo, protected={"release"})
        with pytest.raises(ProtectedBranchError):
            run(manager.merge("x", "release"))

    def test_protected_set_always_includes_the_default(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/trunk\n"
        )
        assert "trunk" in run(MergeManager(fake_repo).protected_branches())


class TestWithMockedGit:
    """Ref checking and merge-tree output parsing."""

    def test_an_unknown_ref_is_reported(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/main\n"
        )
        fake_runner.script("rev-parse --verify --quiet", returncode=1)
        with pytest.raises(MergeError, match="unknown ref"):
            run(MergeManager(fake_repo).merge("ghost", "integration"))

    def test_conflicts_are_parsed_from_merge_tree(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --verify --quiet", stdout="abc\n")
        fake_runner.script(
            "merge-tree --write-tree --name-only",
            returncode=1,
            stdout="treeoid123\nsrc/a.py\nsrc/b.py\n\nCONFLICT (content)\n",
        )
        conflicts = run(MergeManager(fake_repo).detect_conflicts("feature", "target"))
        assert conflicts == ("src/a.py", "src/b.py")

    def test_no_conflicts_on_a_clean_merge_tree(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --verify --quiet", stdout="abc\n")
        fake_runner.script("merge-tree --write-tree --name-only", stdout="treeoid123\n")
        assert run(MergeManager(fake_repo).detect_conflicts("a", "b")) == ()

    def test_an_unexpected_merge_tree_failure_is_reported(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --verify --quiet", stdout="abc\n")
        fake_runner.script(
            "merge-tree --write-tree --name-only", returncode=128, stderr="bad object"
        )
        with pytest.raises(MergeError, match="could not compute"):
            run(MergeManager(fake_repo).detect_conflicts("a", "b"))

    def test_a_conflicting_merge_writes_nothing(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        """FR-3.5: no half-merged index, because nothing is ever applied."""
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/main\n"
        )
        fake_runner.script("rev-parse --verify --quiet", stdout="abc\n")
        fake_runner.script("rev-parse feature", stdout="feature_sha\n")
        fake_runner.script("rev-parse target", stdout="target_sha\n")
        fake_runner.script("merge-base --is-ancestor", returncode=1)
        fake_runner.script(
            "merge-tree --write-tree --name-only",
            returncode=1,
            stdout="tree\nsrc/a.py\n",
        )

        result = run(MergeManager(fake_repo).merge("feature", "target"))

        assert result.status is MergeStatus.CONFLICT
        assert result.conflicted_paths == ("src/a.py",)
        assert not fake_runner.ran("commit-tree")
        assert not fake_runner.ran("update-ref")

    def test_a_clean_merge_uses_a_compare_and_swap_ref_update(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/main\n"
        )
        fake_runner.script("rev-parse --verify --quiet", stdout="abc\n")
        fake_runner.script("rev-parse feature", stdout="feature_sha\n")
        fake_runner.script("rev-parse target", stdout="target_sha\n")
        fake_runner.script("merge-base --is-ancestor", returncode=1)
        fake_runner.script("merge-tree --write-tree --name-only", stdout="tree_oid\n")
        fake_runner.script("-c", stdout="new_sha\n")

        result = run(MergeManager(fake_repo).merge("feature", "target"))

        assert result.status is MergeStatus.MERGED
        update = next(c for c in fake_runner.calls if c[0] == "update-ref")
        # old value passed as the expected state: a concurrent writer cannot be
        # silently clobbered.
        assert update == ("update-ref", "refs/heads/target", "new_sha", "target_sha")

    def test_nothing_to_merge_when_the_branches_match(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", stdout="origin/main\n"
        )
        fake_runner.script("rev-parse --verify --quiet", stdout="abc\n")
        fake_runner.script("rev-parse feature", stdout="same\n")
        fake_runner.script("rev-parse target", stdout="same\n")

        result = run(MergeManager(fake_repo).merge("feature", "target"))
        assert result.status is MergeStatus.NOTHING_TO_MERGE


@requires_git
class TestAgainstRealRepository:
    """Merge semantics — where a mock would agree with a wrong implementation."""

    def _attempt_branch(
        self, temp_repo: TempRepo, run_id: RunId, filename: str, content: str
    ) -> str:
        """Create an attempt branch carrying one committed file."""
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        workspace = run(
            manager.create(run_id=run_id, task_id=TaskId.generate(), attempt=1)
        )
        temp_repo.write(filename, content, cwd=workspace.path)
        run(CommitManager(temp_repo.repository()).commit(workspace, f"Add {filename}"))
        return workspace.branch

    def test_a_clean_merge_advances_the_integration_branch(
        self, temp_repo: TempRepo
    ) -> None:
        repo = temp_repo.repository()
        merger = MergeManager(repo)
        run_id = RunId.generate()
        source = self._attempt_branch(temp_repo, run_id, "a.py", "a\n")

        result = run(merger.integrate_attempt(run_id=run_id, source=source))

        assert result.status is MergeStatus.MERGED
        integration = BranchNamer.integration(run_id)
        assert run(merger.has_merged(source, integration))

    def test_merging_does_not_touch_any_working_tree(
        self, temp_repo: TempRepo
    ) -> None:
        """The whole reason for using plumbing rather than checkout+merge."""
        repo = temp_repo.repository()
        before_branch = run(repo.current_branch())
        before_sha = temp_repo.sha("main")

        run_id = RunId.generate()
        source = self._attempt_branch(temp_repo, run_id, "a.py", "a\n")
        run(MergeManager(repo).integrate_attempt(run_id=run_id, source=source))

        assert run(repo.current_branch()) == before_branch
        assert temp_repo.sha("main") == before_sha
        assert run(repo.is_clean())
        assert not (temp_repo.path / "a.py").exists()

    def test_two_attempts_merge_sequentially(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        merger = MergeManager(repo)
        run_id = RunId.generate()
        first = self._attempt_branch(temp_repo, run_id, "a.py", "a\n")
        second = self._attempt_branch(temp_repo, run_id, "b.py", "b\n")

        integration = run(merger.ensure_integration_branch(run_id))
        results = run(merger.merge_all([first, second], integration))

        assert [r.status for r in results] == [MergeStatus.MERGED, MergeStatus.MERGED]
        assert run(merger.has_merged(first, integration))
        assert run(merger.has_merged(second, integration))

    def test_a_conflict_is_detected_and_nothing_is_applied(
        self, temp_repo: TempRepo
    ) -> None:
        """FR-3.5: conflicted paths reported, no half-merged index left."""
        repo = temp_repo.repository()
        merger = MergeManager(repo)
        run_id = RunId.generate()

        first = self._attempt_branch(temp_repo, run_id, "shared.py", "version one\n")
        second = self._attempt_branch(temp_repo, run_id, "shared.py", "version two\n")

        integration = run(merger.ensure_integration_branch(run_id))
        assert run(merger.merge(first, integration)).status is MergeStatus.MERGED

        before = temp_repo.sha(integration)
        result = run(merger.merge(second, integration))

        assert result.status is MergeStatus.CONFLICT
        assert result.conflicted_paths == ("shared.py",)
        assert temp_repo.sha(integration) == before
        assert run(repo.is_clean())
        # No merge is in progress anywhere.
        assert not (temp_repo.path / ".git" / "MERGE_HEAD").exists()

    def test_detect_conflicts_predicts_without_applying(
        self, temp_repo: TempRepo
    ) -> None:
        repo = temp_repo.repository()
        merger = MergeManager(repo)
        run_id = RunId.generate()
        first = self._attempt_branch(temp_repo, run_id, "shared.py", "one\n")
        second = self._attempt_branch(temp_repo, run_id, "shared.py", "two\n")

        integration = run(merger.ensure_integration_branch(run_id))
        run(merger.merge(first, integration))

        before = temp_repo.sha(integration)
        assert run(merger.detect_conflicts(second, integration)) == ("shared.py",)
        assert temp_repo.sha(integration) == before

    def test_merging_twice_is_reported_as_already_merged(
        self, temp_repo: TempRepo
    ) -> None:
        """Idempotent recovery: the answer comes from Git, not from bookkeeping."""
        repo = temp_repo.repository()
        merger = MergeManager(repo)
        run_id = RunId.generate()
        source = self._attempt_branch(temp_repo, run_id, "a.py", "a\n")
        integration = run(merger.ensure_integration_branch(run_id))

        assert run(merger.merge(source, integration)).status is MergeStatus.MERGED
        second = run(merger.merge(source, integration))
        assert second.status is MergeStatus.ALREADY_MERGED

    def test_the_merge_commit_has_both_parents(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        merger = MergeManager(repo)
        run_id = RunId.generate()
        source = self._attempt_branch(temp_repo, run_id, "a.py", "a\n")
        integration = run(merger.ensure_integration_branch(run_id))

        result = run(merger.merge(source, integration))
        assert result.sha is not None

        parents = temp_repo._git("rev-list", "--parents", "-n", "1", result.sha).stdout
        assert len(parents.split()) == 3

    def test_stop_on_conflict_halts_the_batch(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        merger = MergeManager(repo)
        run_id = RunId.generate()
        first = self._attempt_branch(temp_repo, run_id, "shared.py", "one\n")
        second = self._attempt_branch(temp_repo, run_id, "shared.py", "two\n")
        third = self._attempt_branch(temp_repo, run_id, "c.py", "c\n")

        integration = run(merger.ensure_integration_branch(run_id))
        results = run(
            merger.merge_all([first, second, third], integration, stop_on_conflict=True)
        )

        assert len(results) == 2
        assert results[-1].status is MergeStatus.CONFLICT
        assert not run(merger.has_merged(third, integration))

    def test_merging_into_the_real_default_branch_is_refused(
        self, temp_repo: TempRepo
    ) -> None:
        repo = temp_repo.repository()
        run_id = RunId.generate()
        source = self._attempt_branch(temp_repo, run_id, "a.py", "a\n")
        before = temp_repo.sha("main")

        with pytest.raises(ProtectedBranchError):
            run(MergeManager(repo).merge(source, "main"))
        assert temp_repo.sha("main") == before
