"""Tests for conversation memory and the context manager."""

from __future__ import annotations

import pytest

from orchestrator.agent.memory import (
    ContextManager,
    ContextOverflowError,
    ConversationMemory,
    TranscriptKind,
    estimate_message_tokens,
)
from orchestrator.provider.base import (
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def long_message(role: Role, size: int) -> Message:
    """Build a message of roughly ``size`` characters."""
    return Message(role=role, content=(TextBlock("x" * size),))


class TestConversationMemory:
    """The append-only record of an attempt."""

    def test_starts_empty(self) -> None:
        memory = ConversationMemory()
        assert memory.turn_count == 0
        assert memory.messages() == ()
        assert memory.transcript() == ()

    def test_user_turns_are_appended(self) -> None:
        memory = ConversationMemory()
        memory.add_user("do the thing")
        assert memory.turn_count == 1
        assert memory.messages()[0].role is Role.USER
        assert memory.transcript()[0].kind is TranscriptKind.USER

    def test_assistant_turns_are_stored_verbatim(self) -> None:
        """Several backends reject a history that has been edited."""
        original = Message(
            role=Role.ASSISTANT,
            content=(
                ReasoningBlock("considering"),
                TextBlock("here goes"),
                ToolCallBlock(id="c1", name="read_file", arguments={"path": "a.py"}),
            ),
        )
        memory = ConversationMemory()
        memory.add_assistant(original)
        assert memory.messages()[0] is original

    def test_assistant_blocks_are_transcribed_by_kind(self) -> None:
        memory = ConversationMemory()
        memory.add_assistant(
            Message(
                role=Role.ASSISTANT,
                content=(
                    ReasoningBlock("thinking"),
                    TextBlock("saying"),
                    ToolCallBlock(id="c1", name="read_file"),
                ),
            )
        )
        kinds = [entry.kind for entry in memory.transcript()]
        assert TranscriptKind.REASONING in kinds
        assert TranscriptKind.ASSISTANT in kinds
        assert TranscriptKind.TOOL_CALL in kinds

    def test_empty_reasoning_is_not_transcribed(self) -> None:
        memory = ConversationMemory()
        memory.add_assistant(
            Message(role=Role.ASSISTANT, content=(ReasoningBlock(""), TextBlock("x")))
        )
        assert [e.kind for e in memory.transcript()] == [TranscriptKind.ASSISTANT]

    def test_tool_results_become_one_message(self) -> None:
        """Splitting results across messages suppresses parallel tool calls."""
        memory = ConversationMemory()
        memory.add_tool_results([("c1", "ok", False), ("c2", "bad", True)])

        assert memory.turn_count == 1
        message = memory.messages()[0]
        assert len(message.content) == 2
        assert all(isinstance(b, ToolResultBlock) for b in message.content)

    def test_error_results_keep_their_flag(self) -> None:
        memory = ConversationMemory()
        memory.add_tool_results([("c1", "boom", True)])
        block = memory.messages()[0].content[0]
        assert isinstance(block, ToolResultBlock)
        assert block.is_error

    def test_no_results_adds_no_turn(self) -> None:
        memory = ConversationMemory()
        assert memory.add_tool_results([]) is None
        assert memory.turn_count == 0

    def test_notes_do_not_add_turns(self) -> None:
        """Some facts are for the operator, not the model."""
        memory = ConversationMemory()
        memory.note("dropped 3 turns", dropped=3)

        assert memory.turn_count == 0
        assert memory.transcript()[0].kind is TranscriptKind.NOTE
        assert memory.transcript()[0].detail["dropped"] == 3

    def test_transcript_entries_are_indexed_and_serializable(self) -> None:
        memory = ConversationMemory()
        memory.add_user("a")
        memory.note("b")
        assert [e.index for e in memory.transcript()] == [0, 1]
        assert memory.transcript()[0].to_dict()["kind"] == "user"

    def test_last_assistant_text(self) -> None:
        memory = ConversationMemory()
        memory.add_user("go")
        memory.add_assistant(Message.assistant("first"))
        memory.add_assistant(Message.assistant("second"))
        assert memory.last_assistant_text() == "second"

    def test_last_assistant_text_when_absent(self) -> None:
        memory = ConversationMemory()
        memory.add_user("go")
        assert memory.last_assistant_text() == ""

    def test_estimated_tokens_grow_with_content(self) -> None:
        memory = ConversationMemory()
        memory.add_user("short")
        small = memory.estimated_tokens()
        memory.add_user("x" * 4_000)
        assert memory.estimated_tokens() > small


class TestTokenEstimation:
    """The estimate is deliberately conservative."""

    def test_longer_text_costs_more(self) -> None:
        assert estimate_message_tokens(long_message(Role.USER, 4000)) > (
            estimate_message_tokens(long_message(Role.USER, 40))
        )

    def test_framing_overhead_is_included(self) -> None:
        """An empty message is not free on the wire."""
        assert estimate_message_tokens(Message(role=Role.USER, content=())) > 0

    def test_tool_calls_are_counted(self) -> None:
        with_call = Message(
            role=Role.ASSISTANT,
            content=(ToolCallBlock(id="c1", name="read_file", arguments={"path": "a"}),),
        )
        assert estimate_message_tokens(with_call) > 4

    def test_tool_results_are_counted(self) -> None:
        message = Message(
            role=Role.USER,
            content=(ToolResultBlock(call_id="c1", content="x" * 400),),
        )
        assert estimate_message_tokens(message) > 100


class TestContextManager:
    """NFR-1.4: never truncate silently."""

    def test_everything_fits_when_it_fits(self) -> None:
        messages = [long_message(Role.USER, 100) for _ in range(5)]
        result = ContextManager(reserve_tokens=0).fit(messages, context_limit=100_000)

        assert result.messages == tuple(messages)
        assert not result.truncated
        assert result.strategy == "all"

    def test_reserve_tokens_are_withheld(self) -> None:
        manager = ContextManager(reserve_tokens=1_000)
        assert manager.budget(10_000) == 9_000

    def test_overflow_drops_from_the_middle(self) -> None:
        messages = [long_message(Role.USER, 4_000) for _ in range(20)]
        result = ContextManager(reserve_tokens=0).fit(messages, context_limit=4_000)

        assert result.truncated
        assert result.strategy == "head_and_tail"
        assert result.messages[0] is messages[0]
        assert result.messages[-1] is messages[-1]

    def test_what_was_dropped_is_reported(self) -> None:
        """Silently forgetting an instruction is how agents become inexplicable."""
        messages = [long_message(Role.USER, 4_000) for _ in range(20)]
        result = ContextManager(reserve_tokens=0).fit(messages, context_limit=4_000)

        assert result.dropped > 0
        assert str(result.dropped) in result.note
        assert "dropped" in result.note

    def test_the_kept_set_is_contiguous_at_both_ends(self) -> None:
        messages = [long_message(Role.USER, 2_000) for _ in range(10)]
        result = ContextManager(reserve_tokens=0).fit(messages, context_limit=6_000)

        indices = [messages.index(m) for m in result.messages]
        assert indices == sorted(indices)
        assert indices[0] == 0

    def test_overhead_reduces_the_available_budget(self) -> None:
        messages = [long_message(Role.USER, 2_000) for _ in range(6)]
        generous = ContextManager(reserve_tokens=0).fit(messages, context_limit=20_000)
        constrained = ContextManager(reserve_tokens=0).fit(
            messages, context_limit=20_000, overhead_tokens=18_000
        )
        assert not generous.truncated
        assert constrained.truncated

    def test_an_impossible_overhead_raises(self) -> None:
        """Sending a request known to overflow would be worse than failing."""
        with pytest.raises(ContextOverflowError, match="exceed the model's context"):
            ContextManager(reserve_tokens=0).fit(
                [long_message(Role.USER, 10)], context_limit=100, overhead_tokens=200
            )

    def test_an_irreducible_conversation_raises(self) -> None:
        messages = [long_message(Role.USER, 40_000) for _ in range(3)]
        with pytest.raises(ContextOverflowError, match="cannot be reduced"):
            ContextManager(reserve_tokens=0).fit(messages, context_limit=2_000)

    def test_an_empty_conversation_fits(self) -> None:
        result = ContextManager(reserve_tokens=0).fit([], context_limit=1_000)
        assert result.messages == ()
        assert not result.truncated

    @pytest.mark.parametrize(
        "kwargs", [{"reserve_tokens": -1}, {"keep_recent": 0}]
    )
    def test_nonsensical_settings_are_refused(self, kwargs: dict[str, int]) -> None:
        with pytest.raises(ContextOverflowError):
            ContextManager(**kwargs)  # type: ignore[arg-type]

    def test_fitting_is_deterministic(self) -> None:
        messages = [long_message(Role.USER, 3_000) for _ in range(12)]
        manager = ContextManager(reserve_tokens=0)
        first = manager.fit(messages, context_limit=5_000)
        second = manager.fit(messages, context_limit=5_000)
        assert first == second
