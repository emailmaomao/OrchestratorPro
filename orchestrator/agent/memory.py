"""Conversation memory and the context manager.

Two responsibilities that are easy to conflate and should not be:

:class:`ConversationMemory`
    *What was said.* An append-only record of the turns in one attempt, plus the
    transcript entries that become the durable audit trail.
:class:`ContextManager`
    *What still fits.* Decides which turns to send when the conversation
    outgrows the model's window.

The rule that shapes the second one is NFR-1.4: **never silently truncate**.
Dropping turns quietly is how an agent forgets an instruction and then does
something inexplicable. So :meth:`ContextManager.fit` returns a
:class:`FitResult` naming exactly what it dropped, the runtime records that, and
if not even the minimum fits, it raises rather than sending a request it knows
is wrong.

Token counts here are estimates. Exact counting requires the provider, and the
context manager must be able to make a decision *before* a request is built.
The estimate is deliberately conservative so it errs toward dropping one turn
too many rather than one too few.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from orchestrator.core.events import OrchestratorError
from orchestrator.provider.base import (
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    estimate_tokens,
)

__all__ = [
    "ContextManager",
    "ContextOverflowError",
    "ConversationMemory",
    "FitResult",
    "TranscriptEntry",
    "TranscriptKind",
]

#: Headroom left for the model's own output and for estimate error.
_DEFAULT_RESERVE_TOKENS: Final = 4_000

#: How many of the most recent turns are always kept, budget permitting.
_DEFAULT_KEEP_RECENT: Final = 6


class ContextOverflowError(OrchestratorError):
    """The conversation cannot be made to fit, even after dropping turns."""

    code = "context_overflow"
    retryable = False


class TranscriptKind(StrEnum):
    """What a transcript entry records."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One line of the durable attempt transcript.

    Written as JSONL beside the run rather than into the database: a long
    attempt's history is megabytes, and SQLite is the wrong home for it
    (``ORCHESTRATOR_PRO_SPEC`` §5).
    """

    kind: TranscriptKind
    content: str
    index: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-serializable mapping."""
        return {
            "index": self.index,
            "kind": self.kind.value,
            "content": self.content,
            "detail": dict(self.detail),
        }


class ConversationMemory:
    """The append-only record of one attempt's conversation."""

    __slots__ = ("_messages", "_transcript")

    def __init__(self, messages: Iterable[Message] = ()) -> None:
        """Create the memory, optionally seeded with existing turns."""
        self._messages: list[Message] = list(messages)
        self._transcript: list[TranscriptEntry] = []

    # ---------------------------------------------------------------- writes

    def add_user(self, text: str, *, kind: TranscriptKind = TranscriptKind.USER) -> Message:
        """Append a user turn."""
        message = Message.user(text)
        self._messages.append(message)
        self._record(kind, text)
        return message

    def add_assistant(self, message: Message) -> Message:
        """Append an assistant turn exactly as the provider returned it.

        Stored verbatim: reasoning and tool-call blocks must survive the round
        trip, because several backends reject a history that has been edited.
        """
        self._messages.append(message)
        for block in message.content:
            if isinstance(block, TextBlock) and block.text:
                self._record(TranscriptKind.ASSISTANT, block.text)
            elif isinstance(block, ReasoningBlock) and block.text:
                self._record(TranscriptKind.REASONING, block.text)
            elif isinstance(block, ToolCallBlock):
                self._record(
                    TranscriptKind.TOOL_CALL,
                    block.name,
                    detail={"id": block.id, "arguments": dict(block.arguments)},
                )
        return message

    def add_tool_results(
        self, results: Sequence[tuple[str, str, bool]]
    ) -> Message | None:
        """Append tool results as one user turn.

        Args:
            results: ``(call_id, content, is_error)`` triples.

        Returns:
            The appended message, or ``None`` when there were no results.

        Note:
            Every result for a turn goes into a **single** message. Splitting
            them across messages teaches some backends to stop making parallel
            tool calls.
        """
        if not results:
            return None
        blocks = tuple(
            ToolResultBlock(call_id=call_id, content=content, is_error=is_error)
            for call_id, content, is_error in results
        )
        message = Message(role=Role.USER, content=blocks)
        self._messages.append(message)
        for call_id, content, is_error in results:
            self._record(
                TranscriptKind.TOOL_RESULT,
                content,
                detail={"call_id": call_id, "is_error": is_error},
            )
        return message

    def note(self, text: str, **detail: Any) -> None:
        """Record something in the transcript without adding a turn.

        Used for facts the operator needs and the model does not — a dropped
        turn, a budget warning, a retry boundary.
        """
        self._record(TranscriptKind.NOTE, text, detail=detail)

    def _record(
        self, kind: TranscriptKind, content: str, detail: Mapping[str, Any] | None = None
    ) -> None:
        """Append one transcript entry."""
        self._transcript.append(
            TranscriptEntry(
                kind=kind,
                content=content,
                index=len(self._transcript),
                detail=dict(detail or {}),
            )
        )

    # ----------------------------------------------------------------- reads

    def messages(self) -> tuple[Message, ...]:
        """Every turn, in order."""
        return tuple(self._messages)

    def transcript(self) -> tuple[TranscriptEntry, ...]:
        """Every transcript entry, in order."""
        return tuple(self._transcript)

    @property
    def turn_count(self) -> int:
        """How many turns the conversation holds."""
        return len(self._messages)

    def estimated_tokens(self) -> int:
        """A conservative estimate of the conversation's size."""
        return sum(estimate_message_tokens(message) for message in self._messages)

    def last_assistant_text(self) -> str:
        """Return the most recent assistant text, or ``""``."""
        for message in reversed(self._messages):
            if message.role is Role.ASSISTANT and message.text:
                return message.text
        return ""

    def __len__(self) -> int:
        return len(self._messages)


def estimate_message_tokens(message: Message) -> int:
    """Estimate one message's token cost.

    Deliberately over-counts a little: a per-block overhead is added for the
    envelope the wire format wraps around each block. Erring high means the
    context manager drops a turn early rather than discovering the overflow at
    the provider.
    """
    total = 4  # role and message framing
    for block in message.content:
        total += 4
        if isinstance(block, TextBlock):
            total += estimate_tokens(block.text)
        elif isinstance(block, ReasoningBlock):
            total += estimate_tokens(block.text)
        elif isinstance(block, ToolResultBlock):
            total += estimate_tokens(block.content)
        elif isinstance(block, ToolCallBlock):
            total += estimate_tokens(block.name) + estimate_tokens(repr(block.arguments))
    return total


@dataclass(frozen=True, slots=True)
class FitResult:
    """What the context manager decided to send, and what it left out."""

    messages: tuple[Message, ...]
    dropped: int = 0
    estimated_tokens: int = 0
    strategy: str = "all"
    note: str = ""

    @property
    def truncated(self) -> bool:
        """Whether anything was left out."""
        return self.dropped > 0


class ContextManager:
    """Decides which turns fit inside a model's window.

    The strategy is deliberately simple and explicable: keep the opening turn —
    it carries the task — and as many of the most recent turns as fit, dropping
    from the middle. A middle turn is the least valuable thing to lose, and an
    operator can be told exactly how many went.
    """

    __slots__ = ("_keep_recent", "_reserve_tokens")

    def __init__(
        self,
        *,
        reserve_tokens: int = _DEFAULT_RESERVE_TOKENS,
        keep_recent: int = _DEFAULT_KEEP_RECENT,
    ) -> None:
        """Create the manager.

        Args:
            reserve_tokens: Headroom left for output and estimate error.
            keep_recent: How many recent turns to preserve when possible.

        Raises:
            ContextOverflowError: If the settings are nonsensical.
        """
        if reserve_tokens < 0:
            raise ContextOverflowError("reserve_tokens must not be negative")
        if keep_recent < 1:
            raise ContextOverflowError("keep_recent must be at least 1")
        self._reserve_tokens = reserve_tokens
        self._keep_recent = keep_recent

    @property
    def reserve_tokens(self) -> int:
        """Headroom reserved for output."""
        return self._reserve_tokens

    def budget(self, context_limit: int) -> int:
        """Return the tokens available for conversation history."""
        return max(0, context_limit - self._reserve_tokens)

    def fit(
        self, messages: Sequence[Message], *, context_limit: int, overhead_tokens: int = 0
    ) -> FitResult:
        """Choose the turns to send.

        Args:
            messages: The full conversation, oldest first.
            context_limit: The model's context window, in tokens.
            overhead_tokens: Tokens already committed to the system prompt and
                tool definitions.

        Returns:
            What to send, and what was dropped.

        Raises:
            ContextOverflowError: If not even the opening and most recent turn
                fit. Sending a request known to overflow, or trimming until
                something arbitrary survives, would both be worse.
        """
        available = self.budget(context_limit) - overhead_tokens
        sizes = [estimate_message_tokens(message) for message in messages]
        total = sum(sizes)

        if available <= 0:
            raise ContextOverflowError(
                "the system prompt and tool definitions alone exceed the model's "
                "context window",
                detail={
                    "context_limit": context_limit,
                    "overhead_tokens": overhead_tokens,
                    "reserve_tokens": self._reserve_tokens,
                },
            )
        if total <= available:
            return FitResult(
                messages=tuple(messages), estimated_tokens=total, strategy="all"
            )
        if not messages:  # pragma: no cover - total would be 0 and fit above
            return FitResult(messages=(), estimated_tokens=0, strategy="all")

        # Anchor on the first turn (the task) and the last (what just happened).
        head_index = 0
        kept_indices = [head_index]
        used = sizes[head_index]

        tail_indices: list[int] = []
        for index in range(len(messages) - 1, head_index, -1):
            if used + sizes[index] > available:
                break
            tail_indices.append(index)
            used += sizes[index]

        if not tail_indices:
            raise ContextOverflowError(
                "the conversation cannot be reduced to fit: the opening turn and "
                "the most recent turn do not fit together",
                detail={
                    "context_limit": context_limit,
                    "available_tokens": available,
                    "first_turn_tokens": sizes[head_index],
                    "last_turn_tokens": sizes[-1],
                },
            )

        kept_indices.extend(reversed(tail_indices))
        kept = tuple(messages[index] for index in sorted(set(kept_indices)))
        dropped = len(messages) - len(kept)

        return FitResult(
            messages=kept,
            dropped=dropped,
            estimated_tokens=used,
            strategy="head_and_tail",
            note=(
                f"dropped {dropped} middle turn(s) to fit the context window; "
                f"kept the opening turn and the {len(tail_indices)} most recent"
            ),
        )
