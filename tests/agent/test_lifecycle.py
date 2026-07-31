"""Tests for the agent lifecycle state machine."""

from __future__ import annotations

import itertools

import pytest

from orchestrator.agent.lifecycle import AgentLifecycle, AgentState, LifecycleSummary
from orchestrator.core.events import OrchestratorError, StateTransitionError

from tests.agent.conftest import FakeClock

#: The transition graph, restated so the test fails if the table changes shape.
LEGAL: frozenset[tuple[AgentState, AgentState]] = frozenset(
    {
        (AgentState.IDLE, AgentState.RUNNING),
        (AgentState.IDLE, AgentState.FAILED),
        (AgentState.RUNNING, AgentState.WAITING_TOOL),
        (AgentState.RUNNING, AgentState.COMPLETED),
        (AgentState.RUNNING, AgentState.FAILED),
        (AgentState.WAITING_TOOL, AgentState.RUNNING),
        (AgentState.WAITING_TOOL, AgentState.COMPLETED),
        (AgentState.WAITING_TOOL, AgentState.FAILED),
    }
)


class TestTransitionTable:
    """Transitions are enumerated; anything else raises."""

    @pytest.mark.parametrize(("source", "target"), sorted(LEGAL))
    def test_legal_transitions_are_permitted(
        self, source: AgentState, target: AgentState
    ) -> None:
        assert source.can_transition_to(target)

    @pytest.mark.parametrize(
        ("source", "target"),
        sorted(set(itertools.product(AgentState, repeat=2)) - LEGAL),
    )
    def test_every_other_transition_is_refused(
        self, source: AgentState, target: AgentState
    ) -> None:
        assert not source.can_transition_to(target)

    def test_no_state_transitions_to_itself(self) -> None:
        assert all(not state.can_transition_to(state) for state in AgentState)

    @pytest.mark.parametrize("state", [AgentState.COMPLETED, AgentState.FAILED])
    def test_terminal_states_are_terminal(self, state: AgentState) -> None:
        assert state.is_terminal
        assert all(not state.can_transition_to(other) for other in AgentState)

    def test_active_states(self) -> None:
        assert AgentState.RUNNING.is_active
        assert AgentState.WAITING_TOOL.is_active
        assert not AgentState.IDLE.is_active
        assert not AgentState.COMPLETED.is_active

    def test_idle_cannot_complete_directly(self) -> None:
        """An attempt that never ran cannot have succeeded."""
        assert not AgentState.IDLE.can_transition_to(AgentState.COMPLETED)


class TestLifecycle:
    """The machine records and enforces its own history."""

    def test_starts_idle(self) -> None:
        lifecycle = AgentLifecycle()
        assert lifecycle.state is AgentState.IDLE
        assert lifecycle.history == ()
        assert not lifecycle.is_terminal

    def test_a_full_happy_path(self) -> None:
        lifecycle = AgentLifecycle()
        lifecycle.start()
        lifecycle.await_tools()
        lifecycle.resume()
        lifecycle.complete()

        assert lifecycle.state is AgentState.COMPLETED
        assert lifecycle.is_terminal
        assert lifecycle.path() == (
            "idle",
            "running",
            "waiting_tool",
            "running",
            "completed",
        )

    def test_an_illegal_transition_raises(self) -> None:
        lifecycle = AgentLifecycle()
        with pytest.raises(StateTransitionError) as excinfo:
            lifecycle.await_tools()
        assert excinfo.value.detail["from"] == "idle"
        assert excinfo.value.detail["to"] == "waiting_tool"

    def test_the_error_lists_the_allowed_targets(self) -> None:
        lifecycle = AgentLifecycle()
        lifecycle.start()
        lifecycle.complete()
        with pytest.raises(StateTransitionError) as excinfo:
            lifecycle.resume()
        assert excinfo.value.detail["allowed"] == []

    def test_history_records_every_change(self) -> None:
        lifecycle = AgentLifecycle()
        lifecycle.start(reason="go")
        lifecycle.complete(reason="done")

        assert len(lifecycle.history) == 2
        assert lifecycle.history[0].reason == "go"
        assert lifecycle.history[0].source is AgentState.IDLE
        assert str(lifecycle.history[0]) == "idle -> running (go)"

    def test_a_failure_must_state_a_reason(self) -> None:
        lifecycle = AgentLifecycle()
        with pytest.raises(OrchestratorError, match="must state its reason"):
            lifecycle.fail("   ")

    def test_fail_quietly_is_a_no_op_once_terminal(self) -> None:
        lifecycle = AgentLifecycle()
        lifecycle.start()
        lifecycle.complete()
        assert lifecycle.fail_quietly("cleanup") is None
        assert lifecycle.state is AgentState.COMPLETED

    def test_fail_quietly_still_fails_an_active_attempt(self) -> None:
        lifecycle = AgentLifecycle()
        lifecycle.start()
        change = lifecycle.fail_quietly("provider outage")
        assert change is not None
        assert lifecycle.state is AgentState.FAILED

    def test_an_observer_sees_each_change(self) -> None:
        seen: list[str] = []
        lifecycle = AgentLifecycle(observer=lambda change: seen.append(change.target.value))
        lifecycle.start()
        lifecycle.complete()
        assert seen == ["running", "completed"]

    def test_can_reports_permission_without_transitioning(self) -> None:
        lifecycle = AgentLifecycle()
        assert lifecycle.can(AgentState.RUNNING)
        assert not lifecycle.can(AgentState.COMPLETED)
        assert lifecycle.state is AgentState.IDLE


class TestTiming:
    """Where an attempt's wall-clock time actually went."""

    def test_time_accumulates_per_state(self) -> None:
        clock = FakeClock()
        lifecycle = AgentLifecycle(clock=clock)

        clock.advance(1.0)
        lifecycle.start()
        clock.advance(5.0)
        lifecycle.await_tools()
        clock.advance(20.0)
        lifecycle.resume()
        clock.advance(2.0)
        lifecycle.complete()

        assert lifecycle.time_in(AgentState.IDLE) == pytest.approx(1.0)
        assert lifecycle.time_in(AgentState.RUNNING) == pytest.approx(7.0)
        assert lifecycle.time_in(AgentState.WAITING_TOOL) == pytest.approx(20.0)

    def test_the_current_stay_is_included(self) -> None:
        clock = FakeClock()
        lifecycle = AgentLifecycle(clock=clock)
        lifecycle.start()
        clock.advance(3.0)
        assert lifecycle.time_in(AgentState.RUNNING) == pytest.approx(3.0)

    def test_the_breakdown_distinguishes_thinking_from_waiting(self) -> None:
        """The whole reason waiting_tool is its own state."""
        clock = FakeClock()
        lifecycle = AgentLifecycle(clock=clock)
        lifecycle.start()
        clock.advance(2.0)
        lifecycle.await_tools()
        clock.advance(30.0)
        lifecycle.resume()

        breakdown = lifecycle.breakdown()
        assert breakdown["waiting_tool"] > breakdown["running"]

    def test_summary_captures_the_shape(self) -> None:
        lifecycle = AgentLifecycle(clock=FakeClock())
        lifecycle.start()
        lifecycle.complete()

        summary = LifecycleSummary.of(lifecycle)
        assert summary.state is AgentState.COMPLETED
        assert summary.path == ("idle", "running", "completed")
        assert "running" in summary.breakdown
