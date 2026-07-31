"""Tests for staging, committing, and inspecting attempt work."""

from __future__ import annotations

import pytest

from orchestrator.core.events import AttemptId, RunId, TaskId
from orchestrator.git_manager.commit import CommitError, CommitManager, FileChange
from orchestrator.git_manager.repo import GitRepository
from orchestrator.git_manager.workspace import Workspace, WorkspaceManager

from tests.git_manager.conftest import FakeGitRunner, TempRepo, requires_git, run


def a_workspace(path: str = "/tmp/wt") -> Workspace:
    """Build a workspace value for mocked tests."""
    from pathlib import Path

    return Workspace(
        path=Path(path),
        branch="orchestrator/r/t/1",
        run_id="r",
        task_id="t",
        attempt=1,
    )


class TestFileChange:
    """Porcelain status codes are interpreted, not echoed."""

    def test_untracked_is_recognized(self) -> None:
        assert FileChange(path="a.py", status="??").is_untracked

    def test_deleted_is_recognized(self) -> None:
        assert FileChange(path="a.py", status="D").is_deleted
        assert not FileChange(path="a.py", status="M").is_deleted


class TestWithMockedGit:
    """Translation and refusals, without a repository."""

    def test_changes_parse_porcelain_output(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "status --porcelain", stdout=" M src/a.py\n?? new.txt\nD  gone.py\n"
        )
        changes = run(CommitManager(fake_repo).changes(a_workspace()))
        assert [c.path for c in changes] == ["src/a.py", "new.txt", "gone.py"]

    def test_renames_report_the_new_path(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("status --porcelain", stdout="R  old.py -> new.py\n")
        changes = run(CommitManager(fake_repo).changes(a_workspace()))
        assert changes[0].path == "new.py"

    def test_has_changes_is_false_on_a_clean_tree(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("status --porcelain", stdout="")
        assert not run(CommitManager(fake_repo).has_changes(a_workspace()))

    def test_a_blank_message_is_refused(self, fake_repo: GitRepository) -> None:
        with pytest.raises(CommitError, match="must not be blank"):
            run(CommitManager(fake_repo).commit(a_workspace(), "   "))

    def test_staging_no_paths_is_refused(self, fake_repo: GitRepository) -> None:
        with pytest.raises(CommitError, match="at least one path"):
            run(CommitManager(fake_repo).stage(a_workspace(), []))

    def test_nothing_staged_is_reported_not_raised(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        """An attempt that needed no change is a legitimate outcome."""
        fake_runner.script("status --porcelain", stdout="")
        fake_runner.script("diff --cached --name-only", stdout="")

        result = run(CommitManager(fake_repo).commit(a_workspace(), "no-op"))
        assert result.empty
        assert not result.created
        assert result.sha is None

    def test_commit_runs_in_the_workspace_not_the_repository(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        """A commit must never be aimed at the operator's checkout."""
        fake_runner.script("diff --cached --name-only", stdout="a.py\n")
        workspace = a_workspace()
        run(CommitManager(fake_repo).commit(workspace, "work"))

        commit_calls = [
            index for index, call in enumerate(fake_runner.calls) if "commit" in call
        ]
        assert commit_calls
        for index in commit_calls:
            assert fake_runner.cwds[index] == workspace.path

    def test_identity_is_passed_per_invocation(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        """Mutating the user's git config as a side effect would be rude."""
        fake_runner.script("diff --cached --name-only", stdout="a.py\n")
        run(
            CommitManager(fake_repo, author_name="Bot", author_email="bot@x").commit(
                a_workspace(), "work"
            )
        )
        call = next(c for c in fake_runner.calls if "commit" in c)
        assert "user.name=Bot" in call
        assert "user.email=bot@x" in call
        assert not fake_runner.ran("config")


@requires_git
class TestAgainstRealRepository:
    """Commit semantics against real Git."""

    @pytest.fixture
    def workspace(self, temp_repo: TempRepo) -> Workspace:
        """A real worktree to commit in."""
        manager = WorkspaceManager(
            temp_repo.repository(), root=temp_repo.worktree_root
        )
        return run(
            manager.create(
                run_id=RunId.generate(),
                task_id=TaskId.generate(),
                attempt=1,
                attempt_id=AttemptId.generate(),
            )
        )

    def test_commit_creates_a_commit(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        temp_repo.write("feature.py", "print('hi')\n", cwd=workspace.path)
        manager = CommitManager(temp_repo.repository())

        result = run(manager.commit(workspace, "Add feature"))

        assert result.created
        assert result.sha
        assert "feature.py" in result.files

    def test_committing_nothing_is_not_an_error(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        manager = CommitManager(temp_repo.repository())
        result = run(manager.commit(workspace, "nothing to do"))
        assert result.empty
        assert result.sha is None

    def test_commit_if_changed_skips_a_clean_tree(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        manager = CommitManager(temp_repo.repository())
        assert run(manager.commit_if_changed(workspace, "x")).empty

    def test_trailers_correlate_the_commit_to_its_attempt(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        temp_repo.write("a.py", "x\n", cwd=workspace.path)
        manager = CommitManager(temp_repo.repository())
        run(manager.commit(workspace, "Work"))

        body = temp_repo._git(
            "log", "-1", "--format=%B", cwd=workspace.path
        ).stdout
        assert f"Orchestrator-Run: {workspace.run_id}" in body
        assert f"Orchestrator-Task: {workspace.task_id}" in body
        assert "Orchestrator-Attempt: 1" in body

    def test_deletions_are_staged(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        (workspace.path / "README.md").unlink()
        manager = CommitManager(temp_repo.repository())

        result = run(manager.commit(workspace, "Remove readme"))
        assert result.created
        assert "README.md" in result.files

    def test_changed_files_reports_the_worktree(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        temp_repo.write("one.py", "1\n", cwd=workspace.path)
        temp_repo.write("two.py", "2\n", cwd=workspace.path)
        manager = CommitManager(temp_repo.repository())
        assert run(manager.changed_files(workspace)) == ("one.py", "two.py")

    def test_selective_staging(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        temp_repo.write("keep.py", "1\n", cwd=workspace.path)
        temp_repo.write("skip.py", "2\n", cwd=workspace.path)
        manager = CommitManager(temp_repo.repository())

        run(manager.stage(workspace, ["keep.py"]))
        result = run(manager.commit(workspace, "Partial", stage=False))

        assert result.files == ("keep.py",)
        assert "skip.py" in run(manager.changed_files(workspace))

    def test_the_commit_lands_on_the_attempt_branch_only(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        """FR-3.2 again: main must not move."""
        before = temp_repo.sha("main")
        temp_repo.write("a.py", "x\n", cwd=workspace.path)
        run(CommitManager(temp_repo.repository()).commit(workspace, "Work"))

        assert temp_repo.sha("main") == before
        assert temp_repo.sha(workspace.branch) != before

    def test_diff_names_against_a_base(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        temp_repo.write("a.py", "x\n", cwd=workspace.path)
        manager = CommitManager(temp_repo.repository())
        run(manager.commit(workspace, "Work"))
        assert run(manager.diff_names(workspace, "main")) == ("a.py",)

    def test_log_lists_the_new_commit(
        self, temp_repo: TempRepo, workspace: Workspace
    ) -> None:
        temp_repo.write("a.py", "x\n", cwd=workspace.path)
        manager = CommitManager(temp_repo.repository())
        run(manager.commit(workspace, "A distinctive subject"))
        assert any("A distinctive subject" in line for line in run(manager.log(workspace)))
