"""Tests for repository access and the destructive-operation guard."""

from __future__ import annotations

import pytest

from orchestrator.git_manager.repo import (
    DirtyRepositoryError,
    ForbiddenOperationError,
    GitCommandError,
    GitError,
    GitRepository,
    GitResult,
    NotARepositoryError,
    SubprocessGitRunner,
    classify_operation,
)

from tests.git_manager.conftest import FakeGitRunner, TempRepo, requires_git, run


class TestClassification:
    """Every Git invocation is classified before it runs."""

    @pytest.mark.parametrize(
        "args",
        [
            ("status", "--porcelain"),
            ("rev-parse", "HEAD"),
            ("branch", "orchestrator/r/t/1"),
            ("add", "--all"),
            ("commit", "--message", "x"),
            ("merge-tree", "--write-tree", "a", "b"),
            ("update-ref", "refs/heads/x", "abc", "def"),
            ("worktree", "add", "-b", "b", "/tmp/x"),
            ("worktree", "list", "--porcelain"),
        ],
    )
    def test_ordinary_operations_are_allowed(self, args: tuple[str, ...]) -> None:
        assert classify_operation(args) == "allowed"

    @pytest.mark.parametrize(
        "args",
        [
            ("push", "--force", "origin", "main"),
            ("push", "-f", "origin", "main"),
            ("push", "--force-with-lease", "origin", "main"),
            ("filter-branch", "--all"),
            ("rebase", "main"),
            ("commit", "--amend", "-m", "x"),
            ("reset", "--hard", "HEAD~1"),
        ],
    )
    def test_history_rewrites_are_forbidden(self, args: tuple[str, ...]) -> None:
        """There is no flag that permits these — the spec has no case for them."""
        assert classify_operation(args) == "forbidden"

    @pytest.mark.parametrize(
        "args",
        [
            ("branch", "-d", "x"),
            ("branch", "-D", "x"),
            ("branch", "--delete", "x"),
            ("worktree", "remove", "/tmp/x"),
            ("worktree", "prune"),
            ("clean", "-fd"),
            ("push", "--delete", "origin", "x"),
            ("update-ref", "-d", "refs/heads/x"),
        ],
    )
    def test_destructive_operations_need_authorization(
        self, args: tuple[str, ...]
    ) -> None:
        assert classify_operation(args) == "authorized"

    def test_an_empty_command_is_forbidden(self) -> None:
        assert classify_operation(()) == "forbidden"


class TestGuard:
    """The classification is enforced, not merely computed."""

    def test_allowed_operations_run(self, fake_repo: GitRepository) -> None:
        result = run(fake_repo.run("status", "--porcelain"))
        assert result.ok

    def test_forbidden_operations_are_refused(self, fake_repo: GitRepository) -> None:
        with pytest.raises(ForbiddenOperationError, match="never permitted"):
            run(fake_repo.run("push", "--force", "origin", "main"))

    def test_a_forbidden_operation_never_reaches_git(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        with pytest.raises(ForbiddenOperationError):
            run(fake_repo.run("rebase", "main"))
        assert fake_runner.calls == []

    def test_destructive_operations_need_the_flag(
        self, fake_repo: GitRepository
    ) -> None:
        with pytest.raises(ForbiddenOperationError, match="explicit authorization"):
            run(fake_repo.run("branch", "-D", "x"))

    def test_authorized_destructive_operations_run(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        run(fake_repo.run("branch", "-D", "x", authorize=True))
        assert fake_runner.ran("branch", "-D", "x")

    def test_authorization_does_not_unlock_forbidden_operations(
        self, fake_repo: GitRepository
    ) -> None:
        """No flag enables a force-push."""
        with pytest.raises(ForbiddenOperationError, match="never permitted"):
            run(fake_repo.run("push", "--force", authorize=True))


class TestResults:
    """Command results are translated, not passed through raw."""

    def test_failure_raises_by_default(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("status --porcelain", returncode=1, stderr="boom")
        with pytest.raises(GitCommandError) as excinfo:
            run(fake_repo.run("status", "--porcelain"))
        assert excinfo.value.returncode == 1
        assert "boom" in str(excinfo.value)

    def test_check_false_returns_the_failure(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("status --porcelain", returncode=1, stderr="boom")
        result = run(fake_repo.run("status", "--porcelain", check=False))
        assert not result.ok
        assert result.returncode == 1

    def test_lines_drops_blank_output(self) -> None:
        result = GitResult(args=(), returncode=0, stdout="a\n\n b \n\n")
        assert result.lines() == ("a", " b ")

    def test_out_strips_whitespace(self) -> None:
        assert GitResult(args=(), returncode=0, stdout="  x  \n").out == "x"


class TestReads:
    """Repository queries translate porcelain output into values."""

    def test_is_clean_on_empty_status(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("status --porcelain", stdout="")
        assert run(fake_repo.is_clean())

    def test_is_clean_false_when_dirty(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("status --porcelain", stdout=" M a.py\n?? b.py\n")
        assert not run(fake_repo.is_clean())

    def test_require_clean_raises_and_lists_paths(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("status --porcelain", stdout=" M a.py\n")
        with pytest.raises(DirtyRepositoryError) as excinfo:
            run(fake_repo.require_clean())
        assert excinfo.value.detail["paths"] == [" M a.py"]

    def test_current_branch_reports_empty_when_detached(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --abbrev-ref HEAD", stdout="HEAD\n")
        assert run(fake_repo.current_branch()) == ""

    def test_current_branch_returns_the_name(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --abbrev-ref HEAD", stdout="main\n")
        assert run(fake_repo.current_branch()) == "main"

    def test_toplevel_raises_outside_a_repository(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --show-toplevel", returncode=128, stderr="nope")
        with pytest.raises(NotARepositoryError):
            run(fake_repo.toplevel())

    def test_ref_exists(self, fake_repo: GitRepository, fake_runner: FakeGitRunner) -> None:
        fake_runner.script("rev-parse --verify --quiet refs/heads/x", returncode=1)
        assert not run(fake_repo.ref_exists("refs/heads/x"))

    def test_default_branch_prefers_the_remote_head(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD",
            stdout="origin/trunk\n",
        )
        assert run(fake_repo.default_branch()) == "trunk"

    def test_default_branch_falls_back_to_convention(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD", returncode=1
        )
        fake_runner.script("rev-parse --verify --quiet refs/heads/main", stdout="abc\n")
        assert run(fake_repo.default_branch()) == "main"


class TestSubprocessRunner:
    """The real runner reports missing executables rather than crashing."""

    def test_a_missing_executable_is_reported(self, tmp_dir: object) -> None:
        runner = SubprocessGitRunner(executable="definitely-not-git-xyz")
        with pytest.raises(GitError, match="was not found"):
            run(runner.run(["status"], cwd=__import__("pathlib").Path(".")))


@requires_git
class TestAgainstRealRepository:
    """Semantics that a mock would get wrong in the same way the code does."""

    def test_discovers_a_real_repository(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        assert run(repo.is_repository())
        assert run(repo.toplevel()).resolve() == temp_repo.path.resolve()

    def test_reports_clean_then_dirty(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        assert run(repo.is_clean())

        temp_repo.write("new.txt", "content\n")
        assert not run(repo.is_clean())
        with pytest.raises(DirtyRepositoryError):
            run(repo.require_clean())

    def test_head_and_branch(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        assert run(repo.current_branch()) == "main"
        assert run(repo.head_sha()) == temp_repo.sha()

    def test_ref_existence(self, temp_repo: TempRepo) -> None:
        repo = temp_repo.repository()
        assert run(repo.ref_exists("refs/heads/main"))
        assert not run(repo.ref_exists("refs/heads/nope"))

    def test_default_branch_is_main(self, temp_repo: TempRepo) -> None:
        assert run(temp_repo.repository().default_branch()) == "main"

    def test_a_forbidden_command_is_refused_against_a_real_repo(
        self, temp_repo: TempRepo
    ) -> None:
        repo = temp_repo.repository()
        before = temp_repo.sha()
        with pytest.raises(ForbiddenOperationError):
            run(repo.run("reset", "--hard", "HEAD~1"))
        assert temp_repo.sha() == before
