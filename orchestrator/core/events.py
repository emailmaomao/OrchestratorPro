"""Core foundation primitives and the append-only event model.

This module carries more than its name suggests, deliberately. Milestone M1 was
scoped to five source files, and three primitives — identifiers, the error
taxonomy, and :class:`Budget` — are needed by every other module in the
milestone. Duplicating them across files would have been worse than housing them
beside the event model, which is their heaviest consumer.

The planned split, to be taken as a pure move in the next slice of M1:

* ``orchestrator/core/ids.py``    — :class:`Identifier` and its subclasses
* ``orchestrator/core/errors.py`` — :class:`OrchestratorError` and subclasses
* ``orchestrator/core/budget.py`` — :class:`Budget`, :class:`BudgetAxis`
* ``orchestrator/core/events.py`` — :class:`Event`, :class:`EventType`

Nothing outside this module depends on that layout, so the split costs nothing
but the edit.

The event log is **append-only** and authoritative: per ``docs/020_ARCHITECTURE``
§5, run state is derived from events, and the relational tables are a
materialized view maintained in the same transaction. Serialization here is
therefore deterministic — sorted keys, compact separators — so that an event's
bytes are stable and hashable.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Final, Self

__all__ = [
    "AttemptId",
    "Budget",
    "BudgetAxis",
    "BudgetExhaustedError",
    "ConfigError",
    "DomainValidationError",
    "Event",
    "EventDecodeError",
    "EventId",
    "EventType",
    "Identifier",
    "InvalidIdentifierError",
    "OrchestratorError",
    "RunId",
    "StateTransitionError",
    "TaskId",
]


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #


class OrchestratorError(Exception):
    """Base class for every error this system raises deliberately.

    Two class attributes make errors machine-handleable without string
    matching, per ``CLAUDE.md``:

    ``code``
        A stable, lowercase identifier. Safe to persist, log, and branch on.
        It never changes once released.
    ``retryable``
        Whether retrying the identical operation could plausibly succeed.
        Retrying a non-retryable error burns budget to reproduce a failure.
    """

    code: ClassVar[str] = "internal"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description. Must not contain secrets.
            detail: Structured context for logs and the event payload.
        """
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = dict(detail or {})

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        """Render the error as a JSON-serializable mapping for persistence."""
        return {
            "code": self.code,
            "retryable": self.retryable,
            "message": self.message,
            "detail": self.detail,
        }


class ConfigError(OrchestratorError):
    """Configuration is malformed, contradictory, or forbidden.

    Always raised at startup, never mid-run, and never retried — a bad config
    fails identically on every attempt.
    """

    code = "config"
    retryable = False


class DomainValidationError(OrchestratorError):
    """A domain object was constructed with invalid field values."""

    code = "validation"
    retryable = False


class InvalidIdentifierError(DomainValidationError):
    """A string was not a well-formed identifier of the expected type."""

    code = "invalid_identifier"
    retryable = False


class StateTransitionError(OrchestratorError):
    """An illegal state transition was attempted.

    Raised rather than silently corrected, per ``docs/010_REQUIREMENTS``
    NFR-1.3. A silently corrected transition hides the bug that caused it.
    """

    code = "state_transition"
    retryable = False


class BudgetExhaustedError(OrchestratorError):
    """An attempt consumed its allowance on one of the three budget axes.

    This is a *normal* terminal outcome for an attempt, not a defect. The
    exhausted axis is carried in :attr:`axis` and in ``detail`` so the operator
    can tell a slow task from an expensive one.
    """

    code = "budget_exhausted"
    retryable = False

    def __init__(
        self,
        axis: BudgetAxis,
        *,
        limit: float,
        consumed: float,
        estimated: bool = False,
    ) -> None:
        suffix = " (token counts are estimates)" if estimated else ""
        super().__init__(
            f"budget exhausted on axis {axis.value}: consumed {consumed} of "
            f"{limit}{suffix}",
            detail={
                "axis": axis.value,
                "limit": limit,
                "consumed": consumed,
                "estimated": estimated,
            },
        )
        self.axis = axis
        self.limit = limit
        self.consumed = consumed
        #: Whether the consumption figure is an approximation. A retry decision
        #: made on "consumed 2,000,000 of 2,000,000" should know whether that
        #: number was measured or guessed at four characters per token.
        self.estimated = estimated


class EventDecodeError(OrchestratorError):
    """A persisted event could not be decoded back into an :class:`Event`."""

    code = "event_decode"
    retryable = False


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #

#: Crockford base32 — no I, L, O, or U, so identifiers cannot be misread aloud.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ALPHABET_SET = frozenset(_ALPHABET)

#: 128 bits (48-bit millisecond timestamp + 80 random bits) encodes to 26 chars.
_BODY_LENGTH = 26
_RANDOM_BITS = 80


def _encode(value: int, length: int) -> str:
    """Encode a non-negative integer as fixed-width Crockford base32."""
    out = bytearray(length)
    for i in range(length - 1, -1, -1):
        out[i] = ord(_ALPHABET[value & 0x1F])
        value >>= 5
    return out.decode("ascii")


# Monotonicity state. The timestamp only advances once per millisecond, so
# identifiers minted inside the same millisecond would otherwise sort randomly
# by their random component — defeating the ordering guarantee. Within a
# millisecond the random value is incremented instead of redrawn, which keeps
# order strictly increasing while remaining unguessable across milliseconds.
_id_lock = threading.Lock()
_last_millis = -1
_last_random = 0
_MAX_RANDOM: Final = (1 << _RANDOM_BITS) - 1


def _next_id_value() -> int:
    """Return the next strictly-increasing 128-bit identifier value."""
    global _last_millis, _last_random

    with _id_lock:
        millis = time.time_ns() // 1_000_000
        if millis == _last_millis:
            if _last_random >= _MAX_RANDOM:
                # Exhausting 80 bits of counter inside one millisecond is not
                # reachable in practice; borrowing from the next millisecond
                # keeps the invariant rather than wrapping around.
                _last_millis = millis + 1
                _last_random = int.from_bytes(os.urandom(_RANDOM_BITS // 8), "big")
            else:
                _last_random += 1
        else:
            _last_millis = max(millis, _last_millis)
            _last_random = int.from_bytes(os.urandom(_RANDOM_BITS // 8), "big")
        return (_last_millis << _RANDOM_BITS) | _last_random


class Identifier(str):
    """A sortable, collision-resistant identifier with a type-safe prefix.

    Identifiers are ULID-shaped: a 48-bit millisecond timestamp followed by 80
    random bits, rendered in Crockford base32 behind a domain prefix
    (``run_01J8XK...``). Two properties follow, and both are load-bearing:

    * **Lexicographic order equals chronological order.** Sorting event or
      attempt identifiers as strings sorts them by creation time, which is what
      makes an append-only log cheap to read in order.
    * **Distinct types are distinct classes.** Passing a :class:`TaskId` where a
      :class:`RunId` is expected is visible to a type checker rather than
      surfacing as a lookup miss at runtime.

    Subclassing :class:`str` keeps identifiers directly usable as dict keys,
    JSON values, and path components with no unwrapping.
    """

    __slots__ = ()

    #: Domain prefix, e.g. ``"run"``. Subclasses must set this.
    prefix: ClassVar[str] = ""

    def __new__(cls, raw: str) -> Self:
        """Validate and construct an identifier from its string form.

        Args:
            raw: The full identifier including its prefix.

        Returns:
            The validated identifier.

        Raises:
            InvalidIdentifierError: If ``raw`` is not well-formed for ``cls``.
        """
        if not cls.prefix:
            raise InvalidIdentifierError(
                f"{cls.__name__} does not define a prefix and cannot be instantiated"
            )
        expected = f"{cls.prefix}_"
        if not raw.startswith(expected):
            raise InvalidIdentifierError(
                f"expected an identifier beginning with {expected!r}, got {raw!r}",
                detail={"expected_prefix": cls.prefix},
            )
        body = raw[len(expected) :]
        if len(body) != _BODY_LENGTH:
            raise InvalidIdentifierError(
                f"expected a {_BODY_LENGTH}-character body, got {len(body)} in {raw!r}",
                detail={"expected_length": _BODY_LENGTH, "actual_length": len(body)},
            )
        if not _ALPHABET_SET.issuperset(body):
            raise InvalidIdentifierError(
                f"identifier body contains characters outside the alphabet: {raw!r}"
            )
        return super().__new__(cls, raw)

    @classmethod
    def generate(cls) -> Self:
        """Mint a fresh identifier from the current time and system entropy.

        Successive calls always produce lexicographically increasing values,
        including within a single millisecond.
        """
        return cls(f"{cls.prefix}_{_encode(_next_id_value(), _BODY_LENGTH)}")

    @property
    def created_at(self) -> datetime:
        """The timestamp embedded in the identifier, to millisecond precision."""
        body = self[len(self.prefix) + 1 :]
        value = 0
        for char in body:
            value = (value << 5) | _ALPHABET.index(char)
        milliseconds = value >> _RANDOM_BITS
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


class RunId(Identifier):
    """Identifies one execution of a task graph."""

    __slots__ = ()
    prefix = "run"


class TaskId(Identifier):
    """Identifies one task within a run."""

    __slots__ = ()
    prefix = "task"


class AttemptId(Identifier):
    """Identifies one attempt at a task."""

    __slots__ = ()
    prefix = "att"


class EventId(Identifier):
    """Identifies one entry in the append-only event log."""

    __slots__ = ()
    prefix = "evt"


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


class BudgetAxis(StrEnum):
    """The three independent axes on which an attempt's allowance is capped."""

    SECONDS = "seconds"
    TOKENS = "tokens"
    TOOL_CALLS = "tool_calls"


@dataclass(frozen=True, slots=True)
class Budget:
    """An allowance for a single attempt, capped on three axes at once.

    Whichever axis binds first terminates the attempt (FR-2.6). Three axes are
    needed because they fail differently: a task can spin cheaply for an hour,
    burn a fortune in one turn, or thrash on tool calls without spending much of
    either.

    Note:
        :attr:`tokens` is *OrchestratorPro's own* spend cap. It is unrelated to
        any vendor's thinking-budget parameter and must never be placed in a
        provider request body — see ``CLAUDE.md``.
    """

    seconds: float
    tokens: int
    tool_calls: int

    def __post_init__(self) -> None:
        for name, value in (
            ("seconds", self.seconds),
            ("tokens", self.tokens),
            ("tool_calls", self.tool_calls),
        ):
            if value <= 0:
                raise DomainValidationError(
                    f"budget.{name} must be positive, got {value}",
                    detail={"field": name, "value": value},
                )

    def limit_for(self, axis: BudgetAxis) -> float:
        """Return the configured ceiling for ``axis``."""
        match axis:
            case BudgetAxis.SECONDS:
                return self.seconds
            case BudgetAxis.TOKENS:
                return float(self.tokens)
            case BudgetAxis.TOOL_CALLS:
                return float(self.tool_calls)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class EventType(StrEnum):
    """Every kind of entry that may appear in the append-only log.

    Values are dotted ``subject.verb`` strings, stable once released — they are
    persisted and branched on during replay.
    """

    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_CANCELLED = "run.cancelled"

    # Declares a task's existence and definition. Without it a replay cannot
    # know which tasks a run contains, so the log would not satisfy FR-5.2.
    TASK_CREATED = "task.created"
    TASK_READY = "task.ready"
    TASK_STARTED = "task.started"
    TASK_SUCCEEDED = "task.succeeded"
    TASK_FAILED = "task.failed"
    TASK_BLOCKED = "task.blocked"
    TASK_ABANDONED = "task.abandoned"

    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_FINISHED = "attempt.finished"

    TOOL_CALLED = "tool.called"
    GATE_EVALUATED = "gate.evaluated"

    # Build activity. Recorded for the same reason task events are: a run that
    # spent twenty minutes rebuilding is indistinguishable from one that was
    # stuck unless the log says which units ran and what they produced.
    BUILD_STARTED = "build.started"
    BUILD_UNIT_FINISHED = "build.unit_finished"
    BUILD_FINISHED = "build.finished"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"

    PROVIDER_ERROR = "provider.error"
    INTERNAL_ERROR = "internal.error"


@dataclass(frozen=True, slots=True)
class Event:
    """One immutable entry in the append-only log.

    An event records that something happened. It is never updated and never
    deleted; correcting the record means appending a further event. Because run
    state is reconstructed by replaying these in order, an event must be
    self-contained: a reader replaying the log has no other source of truth.
    """

    id: EventId
    type: EventType
    ts: datetime
    run_id: RunId | None = None
    task_id: TaskId | None = None
    attempt_id: AttemptId | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise DomainValidationError(
                "event timestamp must be timezone-aware; use datetime.now(UTC)"
            )
        try:
            json.dumps(dict(self.payload), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(
                f"event payload is not JSON-serializable: {exc}",
                detail={"event_type": self.type.value},
            ) from exc
        # Defensive copy behind a read-only view: a frozen dataclass whose
        # payload could be mutated in place would not actually be immutable.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def new(
        cls,
        type: EventType,
        *,
        run_id: RunId | None = None,
        task_id: TaskId | None = None,
        attempt_id: AttemptId | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Event:
        """Create an event, minting its identifier and stamping it now (UTC).

        Args:
            type: What happened.
            run_id: The run this event belongs to, if any.
            task_id: The task this event belongs to, if any.
            attempt_id: The attempt this event belongs to, if any.
            payload: JSON-serializable structured detail.

        Returns:
            A fully-formed event ready to append.
        """
        return cls(
            id=EventId.generate(),
            type=type,
            ts=datetime.now(UTC),
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Render the event as a JSON-serializable mapping."""
        return {
            "id": str(self.id),
            "type": self.type.value,
            "ts": self.ts.isoformat(),
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "attempt_id": str(self.attempt_id) if self.attempt_id is not None else None,
            "payload": dict(self.payload),
        }

    def to_json(self) -> str:
        """Serialize deterministically: sorted keys, no incidental whitespace.

        Determinism matters because event bytes are hashed and compared during
        replay verification; incidental key reordering would look like drift.
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        """Reconstruct an event from its mapping form.

        Args:
            data: A mapping previously produced by :meth:`to_dict`.

        Returns:
            The reconstructed event.

        Raises:
            EventDecodeError: If a field is missing or malformed.
        """
        try:
            return cls(
                id=EventId(data["id"]),
                type=EventType(data["type"]),
                ts=datetime.fromisoformat(data["ts"]),
                run_id=RunId(data["run_id"]) if data.get("run_id") else None,
                task_id=TaskId(data["task_id"]) if data.get("task_id") else None,
                attempt_id=AttemptId(data["attempt_id"]) if data.get("attempt_id") else None,
                payload=data.get("payload") or {},
            )
        except (KeyError, ValueError, TypeError, DomainValidationError) as exc:
            raise EventDecodeError(
                f"could not decode event: {exc}",
                detail={"raw_keys": sorted(data)},
            ) from exc

    @classmethod
    def from_json(cls, raw: str) -> Event:
        """Reconstruct an event from its JSON form.

        Args:
            raw: JSON previously produced by :meth:`to_json`.

        Returns:
            The reconstructed event.

        Raises:
            EventDecodeError: If ``raw`` is not valid JSON or not an object.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EventDecodeError(f"event is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise EventDecodeError(
                f"event must decode to an object, got {type(data).__name__}"
            )
        return cls.from_dict(data)
