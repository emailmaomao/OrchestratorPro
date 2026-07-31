"""Tests for identifiers, the error taxonomy, budgets, and the event model."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.core.events import (
    AttemptId,
    Budget,
    BudgetAxis,
    BudgetExhaustedError,
    ConfigError,
    DomainValidationError,
    Event,
    EventDecodeError,
    EventId,
    EventType,
    Identifier,
    InvalidIdentifierError,
    OrchestratorError,
    RunId,
    StateTransitionError,
    TaskId,
)

# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #


class TestErrorTaxonomy:
    """Every error must be machine-handleable without string matching."""

    @pytest.mark.parametrize(
        "cls",
        [
            ConfigError,
            DomainValidationError,
            InvalidIdentifierError,
            StateTransitionError,
            EventDecodeError,
        ],
    )
    def test_every_error_declares_code_and_retryable(self, cls: type[OrchestratorError]) -> None:
        assert isinstance(cls.code, str) and cls.code
        assert isinstance(cls.retryable, bool)

    def test_all_errors_derive_from_the_base(self) -> None:
        for cls in (ConfigError, DomainValidationError, StateTransitionError):
            assert issubclass(cls, OrchestratorError)

    def test_codes_are_unique_across_the_taxonomy(self) -> None:
        codes = [
            ConfigError.code,
            DomainValidationError.code,
            InvalidIdentifierError.code,
            StateTransitionError.code,
            BudgetExhaustedError.code,
            EventDecodeError.code,
        ]
        assert len(codes) == len(set(codes))

    def test_detail_round_trips_through_to_dict(self) -> None:
        err = ConfigError("bad", detail={"key": "run.max_concurrency"})
        assert err.to_dict() == {
            "code": "config",
            "retryable": False,
            "message": "bad",
            "detail": {"key": "run.max_concurrency"},
        }

    def test_budget_exhausted_carries_the_axis(self) -> None:
        err = BudgetExhaustedError(BudgetAxis.TOKENS, limit=100, consumed=140)
        assert err.axis is BudgetAxis.TOKENS
        assert err.detail["axis"] == "tokens"
        assert "tokens" in str(err)


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #


class TestIdentifiers:
    """Identifiers must be prefixed, validated, and chronologically sortable."""

    @pytest.mark.parametrize(
        ("cls", "prefix"),
        [(RunId, "run"), (TaskId, "task"), (AttemptId, "att"), (EventId, "evt")],
    )
    def test_generate_produces_a_well_formed_identifier(
        self, cls: type[Identifier], prefix: str
    ) -> None:
        ident = cls.generate()
        assert ident.startswith(f"{prefix}_")
        assert len(ident) == len(prefix) + 1 + 26
        assert cls(str(ident)) == ident

    def test_identifiers_are_strings(self) -> None:
        assert isinstance(RunId.generate(), str)

    def test_generated_identifiers_are_unique(self) -> None:
        assert len({TaskId.generate() for _ in range(2_000)}) == 2_000

    def test_lexicographic_order_matches_creation_order(self) -> None:
        ids = [TaskId.generate() for _ in range(50)]
        assert sorted(ids) == ids

    def test_wrong_prefix_is_rejected(self) -> None:
        run = RunId.generate()
        with pytest.raises(InvalidIdentifierError):
            TaskId(str(run))

    @pytest.mark.parametrize(
        "raw",
        [
            "task_",
            "task_TOOSHORT",
            "task_0000000000000000000000000000",
            "nope_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "task01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "task_01ARZ3NDEKTSV4RRFFQ69G5FAU",
        ],
    )
    def test_malformed_identifiers_are_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidIdentifierError):
            TaskId(raw)

    def test_base_identifier_cannot_be_instantiated(self) -> None:
        with pytest.raises(InvalidIdentifierError):
            Identifier("anything")

    def test_created_at_recovers_the_embedded_timestamp(self) -> None:
        before = datetime.now(UTC) - timedelta(seconds=2)
        ident = RunId.generate()
        after = datetime.now(UTC) + timedelta(seconds=2)
        assert before <= ident.created_at <= after


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


class TestBudget:
    """Budgets cap three axes at once and reject nonsense values."""

    def test_limit_for_each_axis(self) -> None:
        budget = Budget(seconds=30.0, tokens=1_000, tool_calls=7)
        assert budget.limit_for(BudgetAxis.SECONDS) == 30.0
        assert budget.limit_for(BudgetAxis.TOKENS) == 1_000.0
        assert budget.limit_for(BudgetAxis.TOOL_CALLS) == 7.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"seconds": 0, "tokens": 1, "tool_calls": 1},
            {"seconds": 1, "tokens": 0, "tool_calls": 1},
            {"seconds": 1, "tokens": 1, "tool_calls": 0},
            {"seconds": -1, "tokens": 1, "tool_calls": 1},
        ],
    )
    def test_non_positive_axes_are_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(DomainValidationError):
            Budget(**kwargs)  # type: ignore[arg-type]

    def test_budget_is_immutable(self) -> None:
        budget = Budget(seconds=1.0, tokens=1, tool_calls=1)
        with pytest.raises(AttributeError):
            budget.tokens = 5  # type: ignore[misc]

    def test_every_axis_has_a_limit(self) -> None:
        budget = Budget(seconds=1.0, tokens=1, tool_calls=1)
        for axis in BudgetAxis:
            assert budget.limit_for(axis) > 0


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class TestEvent:
    """Events are immutable, self-contained, and deterministically encoded."""

    def test_new_stamps_id_and_utc_timestamp(self) -> None:
        run = RunId.generate()
        event = Event.new(EventType.RUN_STARTED, run_id=run)
        assert event.id.startswith("evt_")
        assert event.ts.tzinfo is not None
        assert event.run_id == run
        assert event.type is EventType.RUN_STARTED

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(DomainValidationError):
            Event(
                id=EventId.generate(),
                type=EventType.RUN_STARTED,
                ts=datetime.now(),  # noqa: DTZ005 - deliberately naive
            )

    def test_payload_must_be_json_serializable(self) -> None:
        with pytest.raises(DomainValidationError):
            Event.new(EventType.TOOL_CALLED, payload={"when": datetime.now(UTC)})

    def test_payload_is_not_mutable_after_construction(self) -> None:
        payload = {"tool": "read_file"}
        event = Event.new(EventType.TOOL_CALLED, payload=payload)

        payload["tool"] = "mutated"
        assert event.payload["tool"] == "read_file"

        with pytest.raises(TypeError):
            event.payload["tool"] = "mutated"  # type: ignore[index]

    def test_json_round_trip_preserves_the_event(self) -> None:
        original = Event.new(
            EventType.ATTEMPT_FINISHED,
            run_id=RunId.generate(),
            task_id=TaskId.generate(),
            attempt_id=AttemptId.generate(),
            payload={"status": "succeeded", "tokens": 1234, "files": ["a.py", "b.py"]},
        )
        restored = Event.from_json(original.to_json())

        assert restored.id == original.id
        assert restored.type is original.type
        assert restored.ts == original.ts
        assert restored.run_id == original.run_id
        assert restored.task_id == original.task_id
        assert restored.attempt_id == original.attempt_id
        assert dict(restored.payload) == dict(original.payload)

    def test_identifiers_survive_round_trip_as_typed_values(self) -> None:
        event = Event.new(EventType.TASK_STARTED, task_id=TaskId.generate())
        restored = Event.from_json(event.to_json())
        assert isinstance(restored.task_id, TaskId)

    def test_encoding_is_deterministic(self) -> None:
        event = Event.new(
            EventType.GATE_EVALUATED,
            payload={"zebra": 1, "alpha": 2, "middle": 3},
        )
        assert event.to_json() == event.to_json()
        keys = list(json.loads(event.to_json()))
        assert keys == sorted(keys)

    def test_absent_identifiers_encode_as_null(self) -> None:
        event = Event.new(EventType.RUN_CREATED)
        decoded = json.loads(event.to_json())
        assert decoded["run_id"] is None
        assert decoded["task_id"] is None
        assert Event.from_json(event.to_json()).run_id is None

    @pytest.mark.parametrize(
        "raw",
        ["not json at all", "[1, 2, 3]", '{"id": "evt_bad"}', '{"type": "nope.nope"}'],
    )
    def test_malformed_payloads_raise_a_decode_error(self, raw: str) -> None:
        with pytest.raises(EventDecodeError):
            Event.from_json(raw)

    def test_event_is_immutable(self) -> None:
        event = Event.new(EventType.RUN_STARTED)
        with pytest.raises(AttributeError):
            event.type = EventType.RUN_FINISHED  # type: ignore[misc]

    def test_event_types_are_dotted_and_unique(self) -> None:
        values = [member.value for member in EventType]
        assert len(values) == len(set(values))
        assert all("." in value for value in values)
