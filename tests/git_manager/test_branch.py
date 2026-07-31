"""Tests for branch naming, ownership, and safe deletion."""

from __future__ import annotations

import pytest

from orchestrator.core.events import RunId, TaskId
from orchestrator.git_manager.branch import (
    BRANCH_PREFIX,
    BranchManager,
    BranchNamer,
    BranchOwnershipError,
    UnmergedBranchError,
)
from orchestrator.git_manager.repo import GitRepository

from tests.git_manager.conftest import FakeGitRunner, TempRepo, requires_git, run


class TestNaming:
    """The convention is how the system recognizes its own branches."""

    def test_build_follows_the_convention(self) -> None:
        run_id, task_id = RunId.generate(), TaskId.generate()
        name = BranchNamer.build(run_id, task_id, 1)
        assert name == f"{BRANCH_PREFIX}/{run_id}/{task_id}/1"

    def test_built_names_are_recognized_as_owned(self) -> None:
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 3)
        assert BranchNamer.is_owned(name)

    def test_parse_round_trips(self) -> None:
        run_id, task_id = RunId.generate(), TaskId.generate()
        parsed = BranchNamer.parse(BranchNamer.build(run_id, task_id, 7))
        assert parsed is not None
        assert parsed.run_id == str(run_id)
        assert parsed.task_id == str(task_id)
        assert parsed.attempt == 7

    @pytest.mark.parametrize(
        "name",
        [
            "main",
            "feature/login",
            "orchestrator",
            "orchestrator/run/task",
            "orchestrator/run/task/notanumber",
            "orchestrator/run/task/1/extra",
            "prefixorchestrator/r/t/1",
            "",
        ],
    )
    def test_foreign_names_are_not_owned(self, name: str) -> None:
        assert not BranchNamer.is_owned(name)
        assert BranchNamer.parse(name) is None

    def test_a_non_positive_attempt_is_refused(self) -> None:
        with pytest.raises(BranchOwnershipError, match="1-based"):
            BranchNamer.build(RunId.generate(), TaskId.generate(), 0)

    def test_identifiers_with_ref_metacharacters_are_refused(self) -> None:
        """A crafted identifier must not smuggle a refspec or a path traversal."""
        with pytest.raises(BranchOwnershipError, match="invalid branch name"):
            BranchNamer.build("../../evil", "task", 1)

    def test_run_prefix_matches_every_branch_in_a_run(self) -> None:
        run_id = RunId.generate()
        prefix = BranchNamer.run_prefix(run_id)
        assert BranchNamer.build(run_id, TaskId.generate(), 1).startswith(prefix)

    def test_the_integration_branch_is_deliberately_not_owned(self) -> None:
        """It is the operator's deliverable, not disposable bookkeeping."""
        name = BranchNamer.integration(RunId.generate())
        assert name.startswith(BRANCH_PREFIX)
        assert not BranchNamer.is_owned(name)


class TestManagerWithMockedGit:
    """Guard behaviour, without needing a repository."""

    def test_delete_refuses_a_branch_we_did_not_create(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        """FR-3.4: an operator's branch must survive our mistake."""
        manager = BranchManager(fake_repo)
        with pytest.raises(BranchOwnershipError, match="did not create it"):
            run(manager.delete("main"))
        assert not fake_runner.ran("branch", "-d")
        assert not fake_runner.ran("branch", "-D")

    def test_force_does_not_override_ownership(self, fake_repo: GitRepository) -> None:
        manager = BranchManager(fake_repo)
        with pytest.raises(BranchOwnershipError):
            run(manager.delete("release/2026", force=True))

    def test_deleting_a_missing_branch_reports_false(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --verify --quiet refs/heads/", returncode=1)
        manager = BranchManager(fake_repo)
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 1)
        assert run(manager.delete(name)) is False

    def test_an_unmerged_branch_is_protected(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 1)
        fake_runner.script(f"rev-parse --verify --quiet refs/heads/{name}", stdout="abc\n")
        fake_runner.script(f"branch -d {name}", returncode=1, stderr="not fully merged")

        manager = BranchManager(fake_repo)
        with pytest.raises(UnmergedBranchError, match="not merged anywhere"):
            run(manager.delete(name))

    def test_force_deletes_an_unmerged_owned_branch(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 1)
        fake_runner.script(f"rev-parse --verify --quiet refs/heads/{name}", stdout="abc\n")

        manager = BranchManager(fake_repo)
        assert run(manager.delete(name, force=True)) is True
        assert fake_runner.ran("branch", "-D", name)

    def test_create_refuses_an_existing_branch(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script("rev-parse --verify --quiet refs/heads/x", stdout="abc\n")
        with pytest.raises(BranchOwnershipError, match="already exists"):
            run(BranchManager(fake_repo).create("x"))

    def test_list_filters_by_prefix(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "for-each-ref", stdout="main\norchestrator/r1/t1/1\norchestrator/r2/t1/1\n"
        )
        manager = BranchManager(fake_repo)
        assert run(manager.list(prefix="orchestrator/r1/")) == ("orchestrator/r1/t1/1",)

    def test_list_owned_excludes_foreign_branches(
        self, fake_repo: GitRepository, fake_runner: FakeGitRunner
    ) -> None:
        fake_runner.script(
            "for-each-ref",
            stdout="main\nfeature/x\norchestrator/r1/t1/1\norchestrator/r1/integration\n",
        )
        owned = run(BranchManager(fake_repo).list_owned())
        assert owned == ("orchestrator/r1/t1/1",)


@requires_git
class TestManagerAgainstRealRepository:
    """Branch lifecycle against real Git."""

    def test_create_list_and_exists(self, temp_repo: TempRepo) -> None:
        manager = BranchManager(temp_repo.repository())
        run_id, task_id = RunId.generate(), TaskId.generate()
        name = BranchNamer.build(run_id, task_id, 1)

        assert not run(manager.exists(name))
        run(manager.create(name))
        assert run(manager.exists(name))
        assert name in run(manager.list())

    def test_ensure_is_idempotent(self, temp_repo: TempRepo) -> None:
        manager = BranchManager(temp_repo.repository())
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 1)
        assert run(manager.ensure(name)) == name
        assert run(manager.ensure(name)) == name

    def test_deleting_a_merged_branch_succeeds(self, temp_repo: TempRepo) -> None:
        manager = BranchManager(temp_repo.repository())
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 1)
        run(manager.create(name))
        assert run(manager.delete(name)) is True
        assert not run(manager.exists(name))

    def test_deleting_an_unmerged_branch_is_refused(self, temp_repo: TempRepo) -> None:
        """Losing an attempt's only record is exactly what this prevents."""
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 1)
        temp_repo.branch(name)
        temp_repo._git("checkout", "-q", name)
        temp_repo.commit_file("work.txt", "work\n", "attempt work")
        temp_repo._git("checkout", "-q", "main")

        manager = BranchManager(temp_repo.repository())
        with pytest.raises(UnmergedBranchError):
            run(manager.delete(name))
        assert run(manager.exists(name))

        assert run(manager.delete(name, force=True)) is True
        assert not run(manager.exists(name))

    def test_the_operators_branch_survives_a_delete_attempt(
        self, temp_repo: TempRepo
    ) -> None:
        temp_repo.branch("feature/important")
        manager = BranchManager(temp_repo.repository())

        with pytest.raises(BranchOwnershipError):
            run(manager.delete("feature/important", force=True))
        assert run(manager.exists("feature/important"))

    def test_is_merged_into(self, temp_repo: TempRepo) -> None:
        manager = BranchManager(temp_repo.repository())
        name = BranchNamer.build(RunId.generate(), TaskId.generate(), 1)
        run(manager.create(name))
        assert run(manager.is_merged_into(name, "main"))

        temp_repo._git("checkout", "-q", name)
        temp_repo.commit_file("x.txt", "x\n", "diverge")
        temp_repo._git("checkout", "-q", "main")
        assert not run(manager.is_merged_into(name, "main"))

    def test_delete_run_branches_leaves_integration_alone(
        self, temp_repo: TempRepo
    ) -> None:
        run_id = RunId.generate()
        manager = BranchManager(temp_repo.repository())
        for index in range(3):
            run(manager.create(BranchNamer.build(run_id, TaskId.generate(), index + 1)))
        integration = BranchNamer.integration(run_id)
        run(manager.ensure(integration))

        deleted = run(manager.delete_run_branches(run_id))
        assert len(deleted) == 3
        assert run(manager.exists(integration))
