"""Tests for per-attempt worktree isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.events import AttemptId, RunId, TaskId
from orchestrator.git_manager.branch import BranchNamer
from orchestrator.git_manager.repo import GitRepository
from orchestrator.git_manager.workspace import Workspace, WorkspaceError, WorkspaceManager

from tests.git_manager.conftest import FakeGitRunner, TempRepo, requires_git, run


def workspace_at(path: Path, branch: str = "orchestrator/r/t/1") -> Workspace:
    """Build a workspace value pointing at ``path``."""
    return Workspace(path=path, branch=branch, run_id="r", task_id="t", attempt=1)


class TestPathsAndGuards:
    """A worktree manager must never be able to delete the wrong thing."""

    def test_paths_are_scoped_by_run(self, fake_repo: GitRepository, tmp_dir: Path) -> None:
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")
        path = manager.path_for("run1", "task1", 2)
        assert path == tmp_dir / "wt" / "run1" / "task1-2"

    def test_the_default_root_sits_outside_the_repository(
        self, fake_repo: GitRepository
    ) -> None:
        """A run must not litter the working copy the operator is using."""
        manager = WorkspaceManager(fake_repo)
        assert not manager.root.resolve().is_relative_to(fake_repo.path.resolve())

    def test_removing_the_repository_itself_is_refused(
        self, fake_repo: GitRepository, tmp_dir: Path
    ) -> None:
        """FR-3.2: the operator's checkout is never touched."""
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")
        with pytest.raises(WorkspaceError, match="repository's own working tree"):
            run(manager.destroy(workspace_at(fake_repo.path)))

    def test_removing_something_outside_the_root_is_refused(
        self, fake_repo: GitRepository, tmp_dir: Path
    ) -> None:
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")
        with pytest.raises(WorkspaceError, match="outside the worktree root"):
            run(manager.destroy(workspace_at(tmp_dir / "elsewhere")))

    def test_a_non_empty_target_directory_is_refused(
        self, fake_repo: GitRepository, tmp_dir: Path
    ) -> None:
        root = tmp_dir / "wt"
        manager = WorkspaceManager(fake_repo, root=root)
        occupied = manager.path_for("r", "t", 1)
        occupied.mkdir(parents=True)
        (occupied / "stray.txt").write_text("x", encoding="utf-8")

        with pytest.raises(WorkspaceError, match="not empty"):
            run(manager.create(run_id="r", task_id="t", attempt=1))

    def test_isolation_check_detects_a_shared_path(
        self, fake_repo: GitRepository, tmp_dir: Path
    ) -> None:
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")
        shared = tmp_dir / "wt" / "same"
        pair = [workspace_at(shared, "a"), workspace_at(shared, "b")]
        assert not run(manager.is_isolated(pair))

    def test_isolation_check_passes_for_distinct_workspaces(
        self, fake_repo: GitRepository, tmp_dir: Path
    ) -> None:
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")
        pair = [
            workspace_at(tmp_dir / "wt" / "a", "a"),
            workspace_at(tmp_dir / "wt" / "b", "b"),
        ]
        assert run(manager.is_isolated(pair))


class TestCreationCommands:
    """The right Git commands are issued, verified without a repository."""

    def test_create_adds_a_worktree_on_a_new_branch(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner, tmp_dir: Path
    ) -> None:
        fake_runner.script("rev-parse --verify --quiet refs/heads/", returncode=1)
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")

        workspace = run(manager.create(run_id="r", task_id="t", attempt=1))

        assert workspace.branch == BranchNamer.build("r", "t", 1)
        assert fake_runner.ran("worktree", "add", "-b")

    def test_an_existing_branch_is_reused_rather_than_recreated(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner, tmp_dir: Path
    ) -> None:
        """Recovery: a branch that survived a crash must not block the retry."""
        fake_runner.script("rev-parse --verify --quiet refs/heads/", stdout="abc\n")
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")

        run(manager.create(run_id="r", task_id="t", attempt=1))

        assert fake_runner.ran("worktree", "add")
        assert not fake_runner.ran("worktree", "add", "-b")

    def test_the_attempt_id_is_recorded(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner, tmp_dir: Path
    ) -> None:
        fake_runner.script("rev-parse --verify --quiet refs/heads/", returncode=1)
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")
        attempt_id = AttemptId.generate()

        workspace = run(
            manager.create(run_id="r", task_id="t", attempt=1, attempt_id=attempt_id)
        )
        assert workspace.attempt_id == str(attempt_id)

    def test_prune_is_authorized(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner, tmp_dir: Path
    ) -> None:
        manager = WorkspaceManager(fake_repo, root=tmp_dir / "wt")
        run(manager.prune())
        assert fake_runner.ran("worktree", "prune")


@requires_git
class TestAgainstRealRepository:
    """Worktree semantics — the thing a mock would get wrong."""

    def test_a_worktree_is_created_on_its_own_branch(self, temp_repo: TempRepo) -> None:
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        run_id, task_id = RunId.generate(), TaskId.generate()

        workspace = run(manager.create(run_id=run_id, task_id=task_id, attempt=1))

        assert workspace.path.is_dir()
        assert (workspace.path / "README.md").exists()
        assert workspace.branch == BranchNamer.build(run_id, task_id, 1)

    def test_concurrent_worktrees_do_not_interfere(self, temp_repo: TempRepo) -> None:
        """FR-3.1: no two attempts ever share a working tree."""
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        run_id = RunId.generate()
        first = run(manager.create(run_id=run_id, task_id=TaskId.generate(), attempt=1))
        second = run(manager.create(run_id=run_id, task_id=TaskId.generate(), attempt=1))

        assert first.path != second.path
        assert first.branch != second.branch
        assert run(manager.is_isolated([first, second]))

        temp_repo.write("only_first.txt", "a\n", cwd=first.path)
        assert not (second.path / "only_first.txt").exists()

    def test_the_operators_tree_is_untouched(self, temp_repo: TempRepo) -> None:
        """FR-3.2, observed rather than asserted in a docstring."""
        repo = temp_repo.repository()
        before_branch = run(repo.current_branch())
        before_sha = temp_repo.sha()

        manager = WorkspaceManager(repo, root=temp_repo.worktree_root)
        workspace = run(
            manager.create(run_id=RunId.generate(), task_id=TaskId.generate(), attempt=1)
        )
        temp_repo.commit_file("inside.txt", "x\n", "work in worktree", cwd=workspace.path)

        assert run(repo.current_branch()) == before_branch
        assert temp_repo.sha() == before_sha
        assert not (temp_repo.path / "inside.txt").exists()

    def test_list_excludes_the_main_worktree(self, temp_repo: TempRepo) -> None:
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        assert run(manager.list()) == ()

        run(manager.create(run_id="r", task_id="t", attempt=1))
        listed = run(manager.list())
        assert len(listed) == 1
        assert listed[0].resolve() != temp_repo.path.resolve()

    def test_destroy_removes_the_worktree_but_keeps_the_branch(
        self, temp_repo: TempRepo
    ) -> None:
        """A failed attempt's commits are usually its only record."""
        repo = temp_repo.repository()
        manager = WorkspaceManager(repo, root=temp_repo.worktree_root)
        workspace = run(manager.create(run_id="r", task_id="t", attempt=1))

        run(manager.destroy(workspace))

        assert not workspace.path.exists()
        assert run(repo.ref_exists(f"refs/heads/{workspace.branch}"))

    def test_destroy_can_delete_the_branch_too(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        manager = WorkspaceManager(repo, root=temp_repo.worktree_root)
        workspace = run(manager.create(run_id="r", task_id="t", attempt=1))

        run(manager.destroy(workspace, delete_branch=True))

        assert not run(repo.ref_exists(f"refs/heads/{workspace.branch}"))

    def test_a_dirty_worktree_needs_force(self, temp_repo: TempRepo) -> None:
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        workspace = run(manager.create(run_id="r", task_id="t", attempt=1))
        temp_repo.write("dirty.txt", "uncommitted\n", cwd=workspace.path)

        with pytest.raises(WorkspaceError):
            run(manager.destroy(workspace))
        assert workspace.path.exists()

        run(manager.destroy(workspace, force=True))
        assert not workspace.path.exists()

    def test_cleanup_run_removes_every_worktree_for_that_run(
        self, temp_repo: TempRepo
    ) -> None:
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        run_id, other = RunId.generate(), RunId.generate()
        for index in range(2):
            run(manager.create(run_id=run_id, task_id=TaskId.generate(), attempt=index + 1))
        keep = run(manager.create(run_id=other, task_id=TaskId.generate(), attempt=1))

        removed = run(manager.cleanup_run(run_id))

        assert len(removed) == 2
        assert keep.path.exists()

    def test_recreating_after_a_crash_reuses_the_branch(
        self, temp_repo: TempRepo
    ) -> None:
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        first = run(manager.create(run_id="r", task_id="t", attempt=1))
        run(manager.destroy(first))

        again = run(manager.create(run_id="r", task_id="t", attempt=1))
        assert again.branch == first.branch
        assert again.path.is_dir()
