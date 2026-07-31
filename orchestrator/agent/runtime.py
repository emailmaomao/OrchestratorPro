"""The agent runtime and its tool-execution loop.

One :class:`AgentRuntime` performs one attempt: it assembles a prompt, calls a
model, dispatches whatever tools the model asks for, feeds the results back, and
repeats until the task is finished or an allowance runs out.

Four things it deliberately does **not** do:

* **It never decides whether its own work is acceptable.** The result reports
  what happened; gates are applied afterwards by the workflow engine
  (``docs/020_ARCHITECTURE`` §3.1). There is no "passed" field to set.
* **It never names a provider.** Everything goes through
  :class:`~orchestrator.provider.base.ModelPort`, so Claude, a local server, or
  a fake are interchangeable and the tests use the third.
* **It never touches the task graph.** It receives a
  :class:`~orchestrator.agent.model.TaskSpec` — a narrow value object — because
  the ``agent`` and ``task`` packages must not reference each other.
* **It never raises for an ordinary bad outcome.** A refusal, an exhausted
  budget, a provider outage, a cancelled run: each is a terminal
  :class:`~orchestrator.agent.model.AttemptResult` with partial work preserved
  (FR-2.7), because the workflow engine needs to *record* these, not catch them.

The budget is checked on all three axes before every model call and after every
tool call, so an attempt cannot overrun by a whole turn (FR-2.6).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from orchestrator.agent.lifecycle import AgentLifecycle, AgentState, StateChange
from orchestrator.agent.memory import (
    ContextManager,
    ContextOverflowError,
    ConversationMemory,
    TranscriptEntry,
)
from orchestrator.agent.model import (
    AttemptResult,
    AttemptStatus,
    BudgetLedger,
    TaskSpec,
    TokenUsage,
)
from orchestrator.agent.prompt import PromptBuilder
from orchestrator.agent.tools import ToolCall, ToolContext, ToolRegistry, ToolResult
from orchestrator.core.config import Effort, ThinkingMode
from orchestrator.core.events import BudgetExhaustedError
from orchestrator.provider.base import (
    CompletionResponse,
    Message,
    ModelPort,
    ProviderError,
    Role,
    StopReason,
    ToolCallBlock,
)

__all__ = ["AgentRuntime", "RuntimeConfig"]

#: Used when a provider declares no context window of its own.
_FALLBACK_CONTEXT_TOKENS: Final = 200_000


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """How one attempt is run.

    Attributes:
        model: The model identifier, passed through to the provider unread.
        max_iterations: Hard ceiling on model calls. A backstop against a loop
            that never converges — distinct from the token budget, because a
            cheap loop can spin a long time without spending much.
        max_output_tokens: Ceiling on each reply.
        effort: Neutral depth intent.
        reasoning: Neutral reasoning intent.
        context_limit: Override for the provider's declared window.
    """

    model: str
    max_iterations: int = 25
    max_output_tokens: int = 16_000
    effort: Effort = Effort.HIGH
    reasoning: ThinkingMode = ThinkingMode.ADAPTIVE
    context_limit: int | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("runtime.model must not be empty")
        if self.max_iterations < 1:
            raise ValueError(
                f"runtime.max_iterations must be at least 1, got {self.max_iterations}"
            )


class AgentRuntime:
    """Runs one attempt: prompt, model, tools, repeat."""

    __slots__ = (
        "_config",
        "_context",
        "_lifecycle_observer",
        "_prompt",
        "_provider",
        "_registry",
        "_transcript_sink",
    )

    def __init__(
        self,
        provider: ModelPort,
        *,
        config: RuntimeConfig,
        registry: ToolRegistry,
        prompt_builder: PromptBuilder | None = None,
        context_manager: ContextManager | None = None,
        lifecycle_observer: Callable[[StateChange], None] | None = None,
        transcript_sink: Callable[[TranscriptEntry], None] | None = None,
    ) -> None:
        """Create the runtime.

        Args:
            provider: Any model provider. The runtime never learns which.
            config: How to run the attempt.
            registry: The tools the agent may call.
            prompt_builder: Assembles requests. A default is used if omitted.
            context_manager: Decides what fits. A default is used if omitted.
            lifecycle_observer: Called on each state change, for events.
            transcript_sink: Called per transcript entry, for durable capture.
        """
        self._provider = provider
        self._config = config
        self._registry = registry
        self._prompt = prompt_builder or PromptBuilder()
        self._context = context_manager or ContextManager()
        self._lifecycle_observer = lifecycle_observer
        self._transcript_sink = transcript_sink

    @property
    def config(self) -> RuntimeConfig:
        """The runtime's configuration."""
        return self._config

    def _context_limit(self) -> int:
        """Return the usable context window, preferring the provider's own."""
        if self._config.context_limit is not None:
            return self._config.context_limit
        declared = self._provider.capabilities().limit("max_context_tokens", 0)
        return declared or _FALLBACK_CONTEXT_TOKENS

    async def run(
        self,
        spec: TaskSpec,
        ctx: ToolContext,
        ledger: BudgetLedger,
        *,
        transcript_sink: Callable[[TranscriptEntry], None] | None = None,
    ) -> AttemptResult:
        """Perform one attempt.

        Args:
            spec: The task to work on.
            ctx: The tool context, carrying the workspace boundary.
            ledger: The budget to spend against.
            transcript_sink: Receives this attempt's transcript entries, in
                addition to any sink the runtime was constructed with. Per-call
                because one runtime serves many concurrent attempts, and the
                caller — not the runtime — knows which run, task, and attempt
                an entry belongs to (FR-5.3). Flushed in a ``finally``, so a
                caller that cancels the attempt from outside (the wall-clock
                budget) still receives what happened before the cut.

        Returns:
            A terminal result. Never raises for an ordinary bad outcome —
            refusals, exhausted budgets, provider failures, and cancellation all
            come back as results so the caller can record them.
        """
        memory = ConversationMemory()
        try:
            return await self._run(spec, ctx, ledger, memory)
        finally:
            if transcript_sink is not None:
                for entry in memory.transcript():
                    transcript_sink(entry)

    async def _run(
        self,
        spec: TaskSpec,
        ctx: ToolContext,
        ledger: BudgetLedger,
        memory: ConversationMemory,
    ) -> AttemptResult:
        """The attempt loop, over a caller-owned memory."""
        lifecycle = AgentLifecycle(observer=self._lifecycle_observer)
        usage = TokenUsage()
        changed: list[str] = []
        summary = ""

        lifecycle.start()
        for message in self._prompt.opening_messages(spec):
            memory.add_assistant(message) if message.role is Role.ASSISTANT else memory.add_user(
                message.text
            )

        try:
            for iteration in range(self._config.max_iterations):
                if ctx.cancelled():
                    return self._finish(
                        lifecycle, memory, AttemptStatus.CANCELLED, usage, changed,
                        summary="the attempt was cancelled",
                        error_code="cancelled",
                    )

                ledger.check()

                response = await self._one_turn(spec, memory, ledger)
                usage = usage + _usage_of(response)
                memory.add_assistant(
                    Message(role=Role.ASSISTANT, content=response.content)
                )

                if response.stop_reason is StopReason.REFUSED:
                    detail = response.refusal
                    return self._finish(
                        lifecycle, memory, AttemptStatus.REFUSED, usage, changed,
                        summary="the provider declined the request",
                        error_code="refused",
                        extra={
                            "category": detail.category if detail else None,
                            "explanation": detail.explanation if detail else "",
                        },
                    )

                calls = tuple(
                    ToolCall(id=block.id, name=block.name, arguments=block.arguments)
                    for block in response.content
                    if isinstance(block, ToolCallBlock)
                )

                if not calls:
                    summary = response.text.strip() or "the agent ended its turn"
                    memory.note(
                        "the agent ended its turn without calling finish",
                        iteration=iteration,
                    )
                    return self._finish(
                        lifecycle, memory, AttemptStatus.SUCCEEDED, usage, changed,
                        summary=summary,
                    )

                lifecycle.await_tools()
                results = await self._dispatch_all(calls, ctx, ledger)

                memory.add_tool_results(
                    tuple((r.call_id, r.content, r.is_error) for r in results)
                )
                for result in results:
                    changed.extend(
                        path for path in result.changed_paths if path not in changed
                    )

                terminal = next((r for r in results if r.terminal), None)
                if terminal is not None:
                    summary = str(terminal.detail.get("summary", "")) or "task complete"
                    return self._finish(
                        lifecycle, memory, AttemptStatus.SUCCEEDED, usage, changed,
                        summary=summary,
                    )

                lifecycle.resume()

            return self._finish(
                lifecycle, memory, AttemptStatus.BUDGET_EXHAUSTED, usage, changed,
                summary=(
                    f"the attempt made {self._config.max_iterations} model calls "
                    "without finishing"
                ),
                error_code="max_iterations",
            )

        except BudgetExhaustedError as exc:
            return self._finish(
                lifecycle, memory, AttemptStatus.BUDGET_EXHAUSTED, usage, changed,
                summary=str(exc),
                error_code=exc.code,
                extra=dict(exc.detail),
            )
        except ContextOverflowError as exc:
            return self._finish(
                lifecycle, memory, AttemptStatus.ERRORED, usage, changed,
                summary=str(exc),
                error_code=exc.code,
                extra=dict(exc.detail),
            )
        except ProviderError as exc:
            return self._finish(
                lifecycle, memory, AttemptStatus.ERRORED, usage, changed,
                summary=str(exc),
                error_code=exc.error_code.value,
                extra={"retryable": exc.is_retryable, "provider": exc.provider_id},
            )

    async def _one_turn(
        self, spec: TaskSpec, memory: ConversationMemory, ledger: BudgetLedger
    ) -> CompletionResponse:
        """Build a request that fits, send it, and charge its tokens."""
        tools = self._registry.specs()
        overhead = self._prompt.overhead_tokens(spec, tools)
        fit = self._context.fit(
            memory.messages(),
            context_limit=self._context_limit(),
            overhead_tokens=overhead,
        )
        if fit.truncated:
            # NFR-1.4: report, never truncate silently.
            memory.note(fit.note, dropped=fit.dropped, strategy=fit.strategy)

        request = self._prompt.build_request(
            model=self._config.model,
            spec=spec,
            messages=fit.messages,
            tools=tools,
            max_output_tokens=self._config.max_output_tokens,
            effort=self._config.effort,
            reasoning=self._config.reasoning,
        )
        response = await self._provider.complete(request)
        ledger.record_tokens(
            response.usage.total_tokens,
            estimated=response.usage.tokens_estimated,
        )
        return response

    async def _dispatch_all(
        self, calls: Sequence[ToolCall], ctx: ToolContext, ledger: BudgetLedger
    ) -> tuple[ToolResult, ...]:
        """Run every tool call in a turn, charging each against the budget.

        Calls are dispatched in order and every one produces a result, even
        after the budget is exhausted — a turn that answers only some of its
        tool calls is rejected by several backends, so an unanswered call would
        break the next turn rather than ending this one cleanly.
        """
        results: list[ToolResult] = []
        exhausted: BudgetExhaustedError | None = None

        for call in calls:
            if exhausted is not None:
                results.append(
                    ToolResult(
                        call_id=call.id,
                        content="The attempt's budget was exhausted before this "
                        "tool could run.",
                        is_error=True,
                        detail={"code": "budget_exhausted"},
                    )
                )
                continue

            ledger.record_tool_call()
            result = await self._registry.dispatch(call, ctx)
            results.append(replace(result, call_id=call.id))

            try:
                ledger.check()
            except BudgetExhaustedError as exc:
                exhausted = exc

        if exhausted is not None:
            raise exhausted
        return tuple(results)

    def _finish(
        self,
        lifecycle: AgentLifecycle,
        memory: ConversationMemory,
        status: AttemptStatus,
        usage: TokenUsage,
        changed: Sequence[str],
        *,
        summary: str,
        error_code: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> AttemptResult:
        """Close the lifecycle and build the terminal result."""
        if status is AttemptStatus.SUCCEEDED:
            if lifecycle.state is not AgentState.COMPLETED:
                lifecycle.complete(reason=summary[:120])
        else:
            lifecycle.fail_quietly(summary[:120] or status.value)

        self._flush(memory)

        return AttemptResult(
            status=status,
            changed_files=tuple(changed),
            usage=usage,
            summary=summary,
            error_code=error_code,
            detail={
                "lifecycle": list(lifecycle.path()),
                "state_seconds": dict(lifecycle.breakdown()),
                "turns": memory.turn_count,
                **(extra or {}),
            },
        )

    def _flush(self, memory: ConversationMemory) -> None:
        """Hand the transcript to the sink, if one was supplied."""
        if self._transcript_sink is None:
            return
        for entry in memory.transcript():
            self._transcript_sink(entry)


def _usage_of(response: CompletionResponse) -> TokenUsage:
    """Convert a provider's neutral usage into the agent's own."""
    return TokenUsage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cached_input_tokens=response.usage.cached_input_tokens,
        cost_usd=response.usage.cost_usd,
        estimated=response.usage.tokens_estimated,
    )
