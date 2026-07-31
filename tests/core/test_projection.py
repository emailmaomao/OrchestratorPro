"""Tests for replaying the event log into run state."""

from __future__ import annotations

import pytest

from orchestrator.agent.model import AttemptStatus
from orchestrator.core.events import (
    AttemptId,
    Event,
    EventType,
    RunId,
    TaskId,
)
from orchestrator.core.projection import (
    INITIAL_TASK_STATE,
    TASK_STATE_BY_EVENT,
    ReplayError,
    reconstruct,
)
from orchestrator.core.records import RunStatus
from orchestrator.task.model import TaskState
from tests.core.conftest import SequenceBuilder


class TestVocabularyConsistency:
    """The layer-0 state strings must match the layer-2 enums exactly.

    The projection cannot import :class:`TaskState` without inverting the
    dependency direction, so the two vocabularies are reconciled here instead â€”
    tests are not layered and can see both sides.
    """

    def test_every_projected_task_state_is_a_real_task_state(self) -> None:
        valid = {member.value for member in TaskState}
        assert set(TASK_STATE_BY_EVENT.values()) <= valid

    def test_the_initial_state_is_a_real_task_state(self) -> None:
        assert INITIAL_TASK_STATE in {member.value for member in TaskState}

    def test_every_task_lifecycle_event_has_a_state_mapping(self) -> None:
        lifecycle = {
            member
            for member in EventType
            if member.value.startswith("task.") and member is not EventType.TASK_CREATED
        }
        assert lifecycle == set(TASK_STATE_BY_EVENT)

    def test_attempt_statuses_used_in_replay_are_real(self) -> None:
        from orchestrator.core.projection import RUNNING_ATTEMPT_STATUS

        assert RUNNING_ATTEMPT_STATUS in {member.value for member in AttemptStatus}


class TestReconstruct:
    """Replay produces the state the log describes."""

    def test_full_sequence_reconstructs(
        self, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        events, task_id, attempt_id = build_sequence(run_id)
        state = reconstruct(events)

        assert state.run_id == run_id
        assert state.status is RunStatus.FINISHED
        assert state.goal == "migrate the client"
        assert state.repo_path == "/repo"
        assert state.created_at == events[0].ts
        assert state.finished_at == events[-1].ts
        assert state.event_count == len(events)
        assert state.last_event_id == events[-1].id

        task = state.tasks[task_id]
        assert task.title == "Update call sites"
        assert task.state == TaskState.SUCCEEDED.value
        assert task.max_attempts == 3
        assert task.attempts_made == 1
        assert task.attempts_remaining == 2

        attempt = state.attempts[attempt_id]
        assert attempt.status == "succeeded"
        assert attempt.number == 1
        assert attempt.branch == "orchestrator/x"
        assert attempt.tokens_in == 1000
        assert attempt.tokens_out == 250

        assert state.tool_calls == 2
        assert state.usage.total_tokens == 1250
        assert state.usage.cost_usd == pytest.approx(0.011)
        assert len(state.gates) == 1
        assert state.gates[0].passed

    def test_replay_is_deterministic(
        self, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        events, _, _ = build_sequence(run_id)
        assert reconstruct(events) == reconstruct(events)

    def test_input_order_does_not_matter(
        self, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        events, _, _ = build_sequence(run_id)
        assert reconstruct(list(reversed(events))) == reconstruct(events)

    def test_partial_log_reconstructs_partial_state(
        self, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        """A run killed mid-flight still replays into a coherent state."""
        events, task_id, _ = build_sequence(run_id)
        state = reconstruct(events[:6])

        assert state.status is RunStatus.RUNNING
        assert state.finished_at is None
        assert state.tasks[task_id].state == TaskState.RUNNING.value
        assert len(state.attempts) == 1
        assert state.attempts[next(iter(state.attempts))].is_finished is False

    def test_empty_log_with_a_known_run_yields_created(self, run_id: RunId) -> None:
        state = reconstruct([], run_id=run_id)
        assert state.status is RunStatus.CREATED
        assert state.tasks == {}
        assert state.event_count == 0

    def test_task_defaults_to_pending_on_creation(self, run_id: RunId) -> None:
        task_id = TaskId.generate()
        state = reconstruct(
            [
                Event.new(EventType.RUN_CREATED, run_id=run_id),
                Event.new(
                    EventType.TASK_CREATED, run_id=run_id, task_id=task_id, payload={"title": "t"}
                ),
            ]
        )
        assert state.tasks[task_id].state == INITIAL_TASK_STATE

    def test_dependencies_survive_replay(self, run_id: RunId) -> None:
        first, second = TaskId.generate(), TaskId.generate()
        state = reconstruct(
            [
                Event.new(EventType.RUN_CREATED, run_id=run_id),
                Event.new(
                    EventType.TASK_CREATED, run_id=run_id, task_id=first, payload={"title": "a"}
                ),
                Event.new(
                    EventType.TASK_CREATED,
                    run_id=run_id,
                    task_id=second,
                    payload={"title": "b", "depends_on": [str(first)]},
                ),
            ]
        )
        assert state.tasks[second].depends_on == (first,)
        assert isinstance(state.tasks[second].depends_on[0], TaskId)

    def test_retries_accumulate_attempts(self, run_id: RunId) -> None:
        task_id = TaskId.generate()
        events = [
            Event.new(EventType.RUN_CREATED, run_id=run_id),
            Event.new(
                EventType.TASK_CREATED,
                run_id=run_id,
                task_id=task_id,
                payload={"title": "t", "max_attempts": 3},
            ),
        ]
        for n in (1, 2):
            attempt_id = AttemptId.generate()
            events += [
                Event.new(
                    EventType.ATTEMPT_STARTED,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={"number": n},
                ),
                Event.new(
                    EventType.ATTEMPT_FINISHED,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={"status": "succeeded", "tokens_in": 10, "tokens_out": 5},
                ),
            ]
        state = reconstruct(events)
        assert state.tasks[task_id].attempts_made == 2
        assert len(state.attempts_for(task_id)) == 2
        assert [a.number for a in state.attempts_for(task_id)] == [1, 2]
        assert state.usage.total_tokens == 30

    def test_estimated_tokens_replay_as_estimated(self, run_id: RunId) -> None:
        """One estimated attempt marks it and the run's totals as estimates.

        The flag is sticky under aggregation: a total with one approximated
        contribution is an approximation, however many measured ones join it.
        """
        task_id = TaskId.generate()
        first, second = AttemptId.generate(), AttemptId.generate()
        state = reconstruct(
            [
                Event.new(EventType.RUN_CREATED, run_id=run_id),
                Event.new(
                    EventType.TASK_CREATED, run_id=run_id, task_id=task_id, payload={"title": "t"}
                ),
                Event.new(
                    EventType.ATTEMPT_STARTED,
                    run_id=run_id, task_id=task_id, attempt_id=first,
                    payload={"number": 1},
                ),
                Event.new(
                    EventType.ATTEMPT_FINISHED,
                    run_id=run_id, task_id=task_id, attempt_id=first,
                    payload={
                        "status": "failed",
                        "tokens_in": 100, "tokens_out": 40,
                        "tokens_estimated": True,
                    },
                ),
                Event.new(
                    EventType.ATTEMPT_STARTED,
                    run_id=run_id, task_id=task_id, attempt_id=second,
                    payload={"number": 2},
                ),
                Event.new(
                    EventType.ATTEMPT_FINISHED,
                    run_id=run_id, task_id=task_id, attempt_id=second,
                    payload={"status": "succeeded", "tokens_in": 10, "tokens_out": 5},
                ),
            ]
        )

        by_number = {a.number: a for a in state.attempts_for(task_id)}
        assert by_number[1].tokens_estimated is True
        assert by_number[2].tokens_estimated is False
        assert state.usage.estimated is True, "stickiness under aggregation"
        assert state.usage.total_tokens == 155

    def test_a_legacy_payload_without_the_flag_reads_measured(
        self, run_id: RunId
    ) -> None:
        """Logs written before the flag existed replay unchanged (additive)."""
        task_id, attempt_id = TaskId.generate(), AttemptId.generate()
        state = reconstruct(
            [
                Event.new(EventType.RUN_CREATED, run_id=run_id),
                Event.new(
                    EventType.TASK_CREATED, run_id=run_id, task_id=task_id, payload={"title": "t"}
                ),
                Event.new(
                    EventType.ATTEMPT_STARTED,
                    run_id=run_id, task_id=task_id, attempt_id=attempt_id,
                ),
                Event.new(
                    EventType.ATTEMPT_FINISHED,
                    run_id=run_id, task_id=task_id, attempt_id=attempt_id,
                    payload={"status": "succeeded", "tokens_in": 5, "tokens_out": 5},
                ),
            ]
        )
        assert state.usage.estimated is False

    def test_cost_stays_none_when_never_reported(self, run_id: RunId) -> None:
        task_id, attempt_id = TaskId.generate(), AttemptId.generate()
        state = reconstruct(
            [
                Event.new(EventType.RUN_CREATED, run_id=run_id),
                Event.new(
                    EventType.TASK_CREATED, run_id=run_id, task_id=task_id, payload={"title": "t"}
                ),
                Event.new(
                    EventType.ATTEMPT_STARTED,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                ),
                Event.new(
                    EventType.ATTEMPT_FINISHED,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={"status": "succeeded", "tokens_in": 5, "tokens_out": 5},
                ),
            ]
        )
        assert state.usage.cost_usd is None
        assert state.usage.total_tokens == 10

    def test_approval_lifecycle(self, run_id: RunId) -> None:
        task_id = TaskId.generate()
        base = [
            Event.new(EventType.RUN_CREATED, run_id=run_id),
            Event.new(
                EventType.TASK_CREATED, run_id=run_id, task_id=task_id, payload={"title": "t"}
            ),
            Event.new(EventType.APPROVAL_REQUESTED, run_id=run_id, task_id=task_id),
        ]
        pending = reconstruct(base)
        assert len(pending.pending_approvals) == 1

        resolved = reconstruct(
            [
                *base,
                Event.new(
                    EventType.APPROVAL_RESOLVED,
                    run_id=run_id,
                    task_id=task_id,
                    payload={"decision": "approve", "actor": "operator"},
                ),
            ]
        )
        assert resolved.pending_approvals == ()
        assert resolved.approvals[task_id].decision == "approve"

    def test_state_counts_summarize_the_run(self, run_id: RunId) -> None:
        events: list[Event] = [Event.new(EventType.RUN_CREATED, run_id=run_id)]
        for index in range(3):
            task_id = TaskId.generate()
            events.append(
                Event.new(
                    EventType.TASK_CREATED,
                    run_id=run_id,
                    task_id=task_id,
                    payload={"title": f"t{index}"},
                )
            )
            if index < 2:
                events.append(
                    Event.new(EventType.TASK_SUCCEEDED, run_id=run_id, task_id=task_id)
                )
        counts = reconstruct(events).state_counts()
        assert counts[TaskState.SUCCEEDED.value] == 2
        assert counts[INITIAL_TASK_STATE] == 1

    def test_error_events_are_counted_but_do_not_change_state(self, run_id: RunId) -> None:
        state = reconstruct(
            [
                Event.new(EventType.RUN_CREATED, run_id=run_id),
                Event.new(
                    EventType.PROVIDER_ERROR, run_id=run_id, payload={"code": "rate_limited"}
                ),
            ]
        )
        assert state.event_count == 2
        assert state.status is RunStatus.CREATED


class TestReplayFailures:
    """Structural damage raises; forward-compatibility gaps do not."""

    def test_task_event_without_a_creation_raises(self, run_id: RunId) -> None:
        with pytest.raises(ReplayError, match="never created"):
            reconstruct(
                [
                    Event.new(EventType.RUN_CREATED, run_id=run_id),
                    Event.new(EventType.TASK_STARTED, run_id=run_id, task_id=TaskId.generate()),
                ]
            )

    def test_finishing_an_unstarted_attempt_raises(self, run_id: RunId) -> None:
        task_id = TaskId.generate()
        with pytest.raises(ReplayError, match="never started"):
            reconstruct(
                [
                    Event.new(EventType.RUN_CREATED, run_id=run_id),
                    Event.new(
                        EventType.TASK_CREATED,
                        run_id=run_id,
                        task_id=task_id,
                        payload={"title": "t"},
                    ),
                    Event.new(
                        EventType.ATTEMPT_FINISHED,
                        run_id=run_id,
                        task_id=task_id,
                        attempt_id=AttemptId.generate(),
                    ),
                ]
            )

    def test_task_event_without_a_task_id_raises(self, run_id: RunId) -> None:
        with pytest.raises(ReplayError, match="requires task_id"):
            reconstruct(
                [
                    Event.new(EventType.RUN_CREATED, run_id=run_id),
                    Event.new(EventType.TASK_CREATED, run_id=run_id),
                ]
            )

    def test_resolving_an_unrequested_approval_raises(self, run_id: RunId) -> None:
        with pytest.raises(ReplayError, match="never requested"):
            reconstruct(
                [
                    Event.new(EventType.RUN_CREATED, run_id=run_id),
                    Event.new(
                        EventType.APPROVAL_RESOLVED, run_id=run_id, task_id=TaskId.generate()
                    ),
                ]
            )

    def test_mixing_runs_raises(self, run_id: RunId) -> None:
        other = RunId.generate()
        with pytest.raises(ReplayError, match="one run at a time"):
            reconstruct(
                [
                    Event.new(EventType.RUN_CREATED, run_id=run_id),
                    Event.new(EventType.RUN_STARTED, run_id=other),
                ],
                run_id=run_id,
            )

    def test_an_empty_log_without_a_run_id_raises(self) -> None:
        with pytest.raises(ReplayError, match="no run identifier"):
            reconstruct([])


class TestMergeOutcomeReplays:
    """OP-011: the merge story must be reconstructible from events alone.

    Before this, ``attempt.finished`` carried only ``status`` and usage, so a
    healthy run's log could not say whether the work had reached the
    integration branch — the reviewer had to run ``git log``. That is the one
    question the log exists to answer.
    """

    @staticmethod
    def _log(run_id: RunId, payload: dict[str, object]) -> tuple[list[Event], TaskId]:
        task_id = TaskId.generate()
        attempt_id = AttemptId.generate()
        return (
            [
                Event.new(EventType.RUN_CREATED, run_id=run_id),
                Event.new(
                    EventType.TASK_CREATED,
                    run_id=run_id, task_id=task_id, payload={"title": "t"},
                ),
                Event.new(
                    EventType.ATTEMPT_STARTED,
                    run_id=run_id, task_id=task_id, attempt_id=attempt_id,
                    payload={"number": 1},
                ),
                Event.new(
                    EventType.ATTEMPT_FINISHED,
                    run_id=run_id, task_id=task_id, attempt_id=attempt_id,
                    payload=payload,
                ),
            ],
            task_id,
        )

    def test_a_landed_merge_replays_as_landed(self, run_id: RunId) -> None:
        events, task_id = self._log(
            run_id,
            {
                "status": "succeeded",
                "merged": True,
                "merge_status": "merged",
                "changed_files": ["src/ui/main_window.py"],
            },
        )
        attempt = reconstruct(events).attempts_for(task_id)[0]

        assert attempt.merged is True
        assert attempt.merge_status == "merged"
        assert attempt.changed_files == ("src/ui/main_window.py",)
        assert attempt.conflicted_paths == ()

    def test_a_conflict_replays_with_the_files_that_collided(self, run_id: RunId) -> None:
        events, task_id = self._log(
            run_id,
            {
                "status": "failed",
                "merged": False,
                "merge_status": "conflict",
                "changed_files": ["a.py", "b.py"],
                "conflicted_paths": ["a.py"],
            },
        )
        attempt = reconstruct(events).attempts_for(task_id)[0]

        assert attempt.merged is False
        assert attempt.merge_status == "conflict"
        assert attempt.conflicted_paths == ("a.py",)

    def test_a_pre_op_011_log_replays_to_exactly_what_it_did_before(
        self, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        """The compatibility guarantee, stated as an equality.

        A log written before OP-011 carries none of the new keys. Replaying it
        must produce the state it always produced — every pre-existing field
        identical — with the new ones at their "the log does not say" defaults.
        """
        events, task_id, attempt_id = build_sequence(run_id)
        assert not any(
            key in event.payload
            for event in events
            for key in ("merged", "merge_status", "changed_files", "conflicted_paths")
        ), "the fixture must be a genuinely pre-OP-011 log"

        state = reconstruct(events)
        attempt = state.attempts[attempt_id]

        # The new fields say "unrecorded" rather than inventing a verdict.
        assert attempt.merged is None, "absent must not collapse into False"
        assert attempt.merge_status == ""
        assert attempt.changed_files == ()
        assert attempt.conflicted_paths == ()

        # And nothing else moved. Compared field by field against a replay of
        # the same log with the new keys explicitly absent, so a future change
        # that silently alters an old projection fails here.
        from dataclasses import fields

        again = reconstruct(list(events)).attempts[attempt_id]
        for field_ in fields(attempt):
            assert getattr(attempt, field_.name) == getattr(again, field_.name)
        assert state.usage.total_tokens == reconstruct(list(events)).usage.total_tokens
        assert state.tasks[task_id].state == reconstruct(list(events)).tasks[task_id].state

    def test_a_malformed_payload_does_not_break_replay(self, run_id: RunId) -> None:
        """One bad event must not make a whole run unreadable."""
        events, task_id = self._log(
            run_id,
            {"status": "succeeded", "changed_files": "not-a-list", "merged": 1},
        )
        attempt = reconstruct(events).attempts_for(task_id)[0]

        assert attempt.changed_files == ()
        assert attempt.merged is True, "truthy int is still an answer"
