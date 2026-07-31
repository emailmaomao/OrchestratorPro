"""Tests for approvals, attempt history, and transcripts."""

from __future__ import annotations

import pytest

from orchestrator.core.events import AttemptId, Event, EventType, RunId, TaskId
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.workflow.approval import (
    ApprovalDecision,
    ApprovalError,
    ApprovalService,
    decisions_of,
    pending_in,
)


@pytest.fixture
def store() -> RunStore:
    """A run store over a migrated in-memory database."""
    database = Database.in_memory()
    database.migrate()
    return RunStore(database)


@pytest.fixture
def service(store: RunStore) -> ApprovalService:
    """An approval service."""
    return ApprovalService(store)


def seeded(store: RunStore, *, tasks: int = 1) -> tuple[RunId, list[TaskId]]:
    """Create a run with tasks, and return their identifiers."""
    run_id = RunId.generate()
    store.record(
        Event.new(
            EventType.RUN_CREATED,
            run_id=run_id,
            payload={"goal": "review me", "repo_path": "/repo"},
        )
    )
    ids: list[TaskId] = []
    for index in range(tasks):
        task_id = TaskId.generate()
        ids.append(task_id)
        store.record(
            Event.new(
                EventType.TASK_CREATED,
                run_id=run_id,
                task_id=task_id,
                payload={"title": f"task {index}", "prompt": "p", "max_attempts": 3},
            )
        )
    return run_id, ids


def attempt(
    store: RunStore, run_id: RunId, task_id: TaskId, number: int, *, status: str = "failed"
) -> AttemptId:
    """Record one complete attempt."""
    attempt_id = AttemptId.generate()
    store.record(
        Event.new(
            EventType.TASK_STARTED,
            run_id=run_id,
            task_id=task_id,
            payload={"attempt": number},
        )
    )
    store.record(
        Event.new(
            EventType.ATTEMPT_STARTED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={"number": number, "branch": f"orchestrator/{number}"},
        )
    )
    store.record(
        Event.new(
            EventType.TOOL_CALLED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={"tool": "write_file"},
        )
    )
    store.record(
        Event.new(
            EventType.GATE_EVALUATED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={"gate": "unit", "verdict": "failed" if status == "failed" else "passed"},
        )
    )
    store.record(
        Event.new(
            EventType.ATTEMPT_FINISHED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={"status": status, "tokens_in": 100, "tokens_out": 50},
        )
    )
    if status == "failed":
        store.record(
            Event.new(
                EventType.TASK_FAILED,
                run_id=run_id,
                task_id=task_id,
                payload={"attempt": number, "error_code": "gate_failed"},
            )
        )
    return attempt_id


class TestRequesting:
    """Asking for a person."""

    def test_a_request_is_recorded(self, service: ApprovalService, store: RunStore) -> None:
        run_id, tasks = seeded(store)

        request = service.request(run_id, tasks[0], reason="touches the schema")

        assert request.pending
        assert request.reason == "touches the schema"
        assert request.title == "task 0"

    def test_it_is_in_the_log(self, service: ApprovalService, store: RunStore) -> None:
        """An approval is an event, so it replays and cannot be quietly changed."""
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        kinds = [event.type for event in store.events.read_run(run_id)]
        assert EventType.APPROVAL_REQUESTED in kinds

    def test_two_open_requests_are_refused(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        """Two reviewers could otherwise disagree and both be recorded."""
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        with pytest.raises(ApprovalError, match="already waiting"):
            service.request(run_id, tasks[0])

    def test_a_second_request_after_resolution_is_allowed(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])
        service.resolve(run_id, tasks[0], ApprovalDecision.RETRY, actor="alice")

        assert service.request(run_id, tasks[0]).pending


class TestResolving:
    """Deciding."""

    def test_approving_records_the_decision(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        resolved = service.resolve(
            run_id, tasks[0], ApprovalDecision.APPROVED, actor="alice", note="looks right"
        )

        assert not resolved.pending
        assert resolved.decision == "approved"
        assert resolved.actor == "alice"
        assert resolved.note == "looks right"

    def test_rejecting_abandons_the_task(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        service.resolve(run_id, tasks[0], ApprovalDecision.REJECTED, actor="alice")

        assert store.replay(run_id).tasks[tasks[0]].state == "abandoned"

    def test_retrying_puts_the_task_back(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        service.resolve(run_id, tasks[0], ApprovalDecision.RETRY, actor="alice")

        assert store.replay(run_id).tasks[tasks[0]].state == "ready"

    def test_approving_leaves_the_state_alone(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        """The engine carries on; the approval does not move the task itself."""
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="alice")

        assert store.replay(run_id).tasks[tasks[0]].state == "pending"

    def test_an_unattributed_approval_is_refused(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        """An approval nobody made is not an approval."""
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        with pytest.raises(ApprovalError, match="who made it"):
            service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="  ")

    def test_resolving_nothing_is_refused(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)

        with pytest.raises(ApprovalError, match="not waiting"):
            service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="a")

    def test_resolving_twice_is_refused(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])
        service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="alice")

        with pytest.raises(ApprovalError, match="already approved by alice"):
            service.resolve(run_id, tasks[0], ApprovalDecision.REJECTED, actor="bob")

    def test_the_decision_survives_a_replay(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])
        service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="alice")

        reread = ApprovalService(store).for_task(run_id, tasks[0])
        assert reread is not None
        assert reread.actor == "alice"


class TestQueue:
    """What is waiting."""

    def test_an_empty_queue(self, service: ApprovalService) -> None:
        assert service.queue() == ()

    def test_pending_requests_appear(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store, tasks=2)
        service.request(run_id, tasks[0])
        service.request(run_id, tasks[1])

        assert len(service.queue()) == 2

    def test_resolved_requests_do_not(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store, tasks=2)
        service.request(run_id, tasks[0])
        service.request(run_id, tasks[1])
        service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="a")

        assert len(service.queue()) == 1

    def test_it_is_oldest_first(self, service: ApprovalService, store: RunStore) -> None:
        """A queue sorted newest-first has a bottom nobody looks at."""
        run_id, tasks = seeded(store, tasks=3)
        for task_id in tasks:
            service.request(run_id, task_id)

        queued = service.queue()
        assert [r.requested_at for r in queued] == sorted(r.requested_at for r in queued)

    def test_it_spans_runs(self, service: ApprovalService, store: RunStore) -> None:
        first, first_tasks = seeded(store)
        second, second_tasks = seeded(store)
        service.request(first, first_tasks[0])
        service.request(second, second_tasks[0])

        assert len({r.run_id for r in service.queue()}) == 2

    def test_the_limit_is_honoured(self, service: ApprovalService, store: RunStore) -> None:
        run_id, tasks = seeded(store, tasks=5)
        for task_id in tasks:
            service.request(run_id, task_id)

        assert len(service.queue(limit=2)) == 2

    def test_decisions_are_counted(self, service: ApprovalService, store: RunStore) -> None:
        run_id, tasks = seeded(store, tasks=3)
        for task_id in tasks:
            service.request(run_id, task_id)
        service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="a")
        service.resolve(run_id, tasks[1], ApprovalDecision.REJECTED, actor="a")

        counts = decisions_of(service.for_run(run_id))
        assert counts == {"pending": 1, "approved": 1, "rejected": 1}


class TestAttemptHistory:
    """What a reviewer needs to decide."""

    def test_every_attempt_is_listed(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)
        attempt(store, run_id, tasks[0], 2)

        history = service.history(run_id, tasks[0])
        assert [a.number for a in history.attempts] == [1, 2]

    def test_an_attempt_reports_its_gates(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        latest = service.history(run_id, tasks[0]).latest
        assert latest is not None
        assert latest.failing_gates == ("unit",)

    def test_an_attempt_reports_its_error(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        assert service.history(run_id, tasks[0]).attempts[0].error_code == "gate_failed"

    def test_usage_is_reported(self, service: ApprovalService, store: RunStore) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        summary = service.history(run_id, tasks[0]).attempts[0]
        assert summary.tokens_in == 100
        assert summary.tokens_out == 50

    def test_a_repeated_failure_is_surfaced(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        """The thing a reviewer most wants at attempt three: same wall or new one."""
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)
        attempt(store, run_id, tasks[0], 2)

        assert service.history(run_id, tasks[0]).repeated_failures == ("unit",)

    def test_a_single_failure_is_not_repeated(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        assert service.history(run_id, tasks[0]).repeated_failures == ()

    def test_a_successful_attempt_fails_no_gates(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1, status="succeeded")

        assert service.history(run_id, tasks[0]).attempts[0].failing_gates == ()

    def test_the_branch_is_recorded(self, service: ApprovalService, store: RunStore) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        assert service.history(run_id, tasks[0]).attempts[0].branch == "orchestrator/1"

    def test_an_unknown_task_is_refused(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, _ = seeded(store)

        with pytest.raises(ApprovalError, match="no task"):
            service.history(run_id, TaskId.generate())

    def test_a_task_with_no_attempts_has_an_empty_history(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        history = service.history(run_id, tasks[0])

        assert history.attempts == ()
        assert history.latest is None


class TestTranscript:
    """What one attempt did."""

    def test_the_events_are_in_order(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        transcript = service.transcript(run_id, tasks[0])
        kinds = [entry.type for entry in transcript.entries]

        assert kinds.index("attempt.started") < kinds.index("tool.called")
        assert kinds.index("tool.called") < kinds.index("attempt.finished")

    def test_tool_calls_are_counted(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        assert service.transcript(run_id, tasks[0]).tool_calls == 1

    def test_each_entry_reads_as_a_sentence(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        summaries = [entry.summary for entry in service.transcript(run_id, tasks[0]).entries]
        assert "called write_file" in summaries
        assert "gate unit: failed" in summaries

    def test_it_can_be_scoped_to_one_attempt(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        first = attempt(store, run_id, tasks[0], 1)
        attempt(store, run_id, tasks[0], 2)

        scoped = service.transcript(run_id, tasks[0], attempt_id=first)
        attempt_ids = {
            entry.payload.get("number") for entry in scoped.entries if "number" in entry.payload
        }
        assert attempt_ids == {1}

    def test_it_carries_the_payloads(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        attempt(store, run_id, tasks[0], 1)

        tool = [e for e in service.transcript(run_id, tasks[0]).entries if e.type == "tool.called"]
        assert tool[0].payload["tool"] == "write_file"

    def test_an_approval_appears_in_the_transcript(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0], reason="schema change")
        service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="alice")

        summaries = [entry.summary for entry in service.transcript(run_id, tasks[0]).entries]
        assert "approval requested: schema change" in summaries
        assert "approved by alice" in summaries


class TestPendingIn:
    """The engine's shortcut."""

    def test_it_finds_a_pending_request(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])

        assert pending_in(store.replay(run_id)) == (tasks[0],)

    def test_a_resolved_request_is_not_pending(
        self, service: ApprovalService, store: RunStore
    ) -> None:
        run_id, tasks = seeded(store)
        service.request(run_id, tasks[0])
        service.resolve(run_id, tasks[0], ApprovalDecision.APPROVED, actor="a")

        assert pending_in(store.replay(run_id)) == ()


class TestDecisionSemantics:
    """The vocabulary."""

    def test_only_approval_accepts(self) -> None:
        assert ApprovalDecision.APPROVED.accepts
        assert not ApprovalDecision.REJECTED.accepts
        assert not ApprovalDecision.RETRY.accepts

    def test_only_retry_reopens(self) -> None:
        assert ApprovalDecision.RETRY.reopens
        assert not ApprovalDecision.APPROVED.reopens
