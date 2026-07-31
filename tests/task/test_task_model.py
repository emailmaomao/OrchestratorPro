"""Tests for the task domain model and its state machine."""

from __future__ import annotations

import itertools

import pytest

from orchestrator.core.events import Budget, DomainValidationError, StateTransitionError, TaskId
from orchestrator.task.model import GateKind, GateSpec, Task, TaskState


@pytest.fixture
def budget() -> Budget:
    """A small, valid budget for constructing tasks."""
    return Budget(seconds=60.0, tokens=1_000, tool_calls=10)


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #

#: The transition graph from ORCHESTRATOR_PRO_SPEC §4.1, restated here so the
#: test fails if the implementation quietly changes shape.
LEGAL: frozenset[tuple[TaskState, TaskState]] = frozenset(
    {
        (TaskState.PENDING, TaskState.READY),
        (TaskState.PENDING, TaskState.BLOCKED),
        (TaskState.PENDING, TaskState.ABANDONED),
        (TaskState.READY, TaskState.RUNNING),
        (TaskState.READY, TaskState.ABANDONED),
        (TaskState.RUNNING, TaskState.GATING),
        (TaskState.RUNNING, TaskState.FAILED),
        (TaskState.RUNNING, TaskState.ABANDONED),
        (TaskState.GATING, TaskState.SUCCEEDED),
        (TaskState.GATING, TaskState.FAILED),
        (TaskState.FAILED, TaskState.RETRYING),
        (TaskState.FAILED, TaskState.ABANDONED),
        (TaskState.RETRYING, TaskState.READY),
        (TaskState.RETRYING, TaskState.ABANDONED),
    }
)


class TestTaskState:
    """Transitions are explicitly enumerated; anything else raises."""

    @pytest.mark.parametrize(("source", "target"), sorted(LEGAL))
    def test_legal_transitions_are_permitted(
        self, source: TaskState, target: TaskState
    ) -> None:
        assert source.can_transition_to(target)
        source.assert_transition(target)

    @pytest.mark.parametrize(
        ("source", "target"),
        sorted(set(itertools.product(TaskState, repeat=2)) - LEGAL),
    )
    def test_every_other_transition_is_refused(
        self, source: TaskState, target: TaskState
    ) -> None:
        assert not source.can_transition_to(target)
        with pytest.raises(StateTransitionError):
            source.assert_transition(target)

    @pytest.mark.parametrize(
        "state", [TaskState.SUCCEEDED, TaskState.BLOCKED, TaskState.ABANDONED]
    )
    def test_terminal_states_are_terminal(self, state: TaskState) -> None:
        assert state.is_terminal
        assert all(not state.can_transition_to(other) for other in TaskState)

    @pytest.mark.parametrize(
        "state",
        [
            TaskState.PENDING,
            TaskState.READY,
            TaskState.RUNNING,
            TaskState.GATING,
            TaskState.RETRYING,
            TaskState.FAILED,
        ],
    )
    def test_non_terminal_states_have_a_way_forward(self, state: TaskState) -> None:
        assert not state.is_terminal
        assert any(state.can_transition_to(other) for other in TaskState)

    def test_failed_is_not_terminal(self) -> None:
        """A failed task with attempts left retries; failure is not the end."""
        assert not TaskState.FAILED.is_terminal
        assert TaskState.FAILED.can_transition_to(TaskState.RETRYING)

    def test_no_state_transitions_to_itself(self) -> None:
        assert all(not state.can_transition_to(state) for state in TaskState)

    def test_error_names_the_allowed_targets(self) -> None:
        task_id = TaskId.generate()
        with pytest.raises(StateTransitionError) as excinfo:
            TaskState.SUCCEEDED.assert_transition(TaskState.RUNNING, task_id=task_id)
        detail = excinfo.value.detail
        assert detail["from"] == "succeeded"
        assert detail["to"] == "running"
        assert detail["task_id"] == str(task_id)

    def test_a_full_happy_path_is_walkable(self) -> None:
        path = [
            TaskState.PENDING,
            TaskState.READY,
            TaskState.RUNNING,
            TaskState.GATING,
            TaskState.SUCCEEDED,
        ]
        for source, target in itertools.pairwise(path):
            source.assert_transition(target)

    def test_a_full_retry_path_is_walkable(self) -> None:
        path = [
            TaskState.PENDING,
            TaskState.READY,
            TaskState.RUNNING,
            TaskState.GATING,
            TaskState.FAILED,
            TaskState.RETRYING,
            TaskState.READY,
            TaskState.RUNNING,
        ]
        for source, target in itertools.pairwise(path):
            source.assert_transition(target)


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


class TestGateSpec:
    """Gates are named checks, required by default."""

    def test_defaults_to_a_required_test_gate(self) -> None:
        gate = GateSpec(name="unit-tests")
        assert gate.kind is GateKind.TEST
        assert gate.required is True

    def test_blank_name_is_rejected(self) -> None:
        with pytest.raises(DomainValidationError):
            GateSpec(name="   ")

    def test_advisory_gates_can_be_declared(self) -> None:
        gate = GateSpec(name="lint", kind=GateKind.LINT, required=False)
        assert gate.required is False


# --------------------------------------------------------------------------- #
# Task
# --------------------------------------------------------------------------- #


class TestTask:
    """Tasks are immutable, validated value objects."""

    def test_create_mints_an_identifier(self, budget: Budget) -> None:
        task = Task.create(title="Migrate client", prompt="Do the thing", budget=budget)
        assert task.id.startswith("task_")
        assert task.is_root
        assert task.max_attempts == 3

    def test_task_is_immutable(self, budget: Budget) -> None:
        task = Task.create(title="t", prompt="p", budget=budget)
        with pytest.raises(AttributeError):
            task.title = "changed"  # type: ignore[misc]

    @pytest.mark.parametrize(("title", "prompt"), [("", "p"), ("   ", "p"), ("t", ""), ("t", "  ")])
    def test_blank_title_or_prompt_is_rejected(
        self, budget: Budget, title: str, prompt: str
    ) -> None:
        with pytest.raises(DomainValidationError):
            Task.create(title=title, prompt=prompt, budget=budget)

    @pytest.mark.parametrize("attempts", [0, -1])
    def test_max_attempts_must_be_at_least_one(self, budget: Budget, attempts: int) -> None:
        with pytest.raises(DomainValidationError, match="max_attempts"):
            Task.create(title="t", prompt="p", budget=budget, max_attempts=attempts)

    def test_self_dependency_is_rejected(self, budget: Budget) -> None:
        task_id = TaskId.generate()
        with pytest.raises(DomainValidationError, match="itself"):
            Task(id=task_id, title="t", prompt="p", budget=budget, depends_on=(task_id,))

    def test_duplicate_dependencies_are_rejected(self, budget: Budget) -> None:
        dep = TaskId.generate()
        with pytest.raises(DomainValidationError, match="duplicates"):
            Task.create(title="t", prompt="p", budget=budget, depends_on=(dep, dep))

    def test_duplicate_gate_names_are_rejected(self, budget: Budget) -> None:
        with pytest.raises(DomainValidationError, match="unique names"):
            Task.create(
                title="t",
                prompt="p",
                budget=budget,
                gates=(GateSpec(name="tests"), GateSpec(name="tests")),
            )

    def test_dependencies_make_a_task_non_root(self, budget: Budget) -> None:
        task = Task.create(
            title="t", prompt="p", budget=budget, depends_on=(TaskId.generate(),)
        )
        assert not task.is_root

    def test_required_gates_excludes_advisory_ones(self, budget: Budget) -> None:
        task = Task.create(
            title="t",
            prompt="p",
            budget=budget,
            gates=(
                GateSpec(name="tests", required=True),
                GateSpec(name="lint", kind=GateKind.LINT, required=False),
            ),
        )
        assert [gate.name for gate in task.required_gates] == ["tests"]

    def test_labels_are_queryable(self, budget: Budget) -> None:
        task = Task.create(title="t", prompt="p", budget=budget, labels=("db", "slow"))
        assert task.has_label("db")
        assert not task.has_label("fast")

    def test_task_does_not_import_the_agent_package(self) -> None:
        """Layering: task and agent are siblings that never reference each other."""
        import ast
        from pathlib import Path

        import orchestrator.task.model as module

        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not any(name.startswith("orchestrator.agent") for name in imported)
