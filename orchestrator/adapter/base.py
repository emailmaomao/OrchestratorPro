"""The agent-backend seam: *how* an attempt is performed.

``docs/030_PROVIDER_INTERFACE.md`` §5.2 specifies this domain and
``orchestrator/adapter/`` has been an empty package since v1.0, waiting for a
second harness to justify declaring it. The Claude Code harness is that second
harness, so the protocol is written now — from two real implementations rather
than from one and an imagination, which is the whole reason §5.2 said to wait.

Two implementations satisfy it today:

* :class:`~orchestrator.agent.runtime.AgentRuntime` — our own tool loop: the
  model proposes tool calls, we execute them inside a confined workspace, and
  every call is audited.
* :class:`~orchestrator.adapter.claude_code.ClaudeCodeHarness` — an external
  harness that edits the worktree itself. Less resolution, no confinement of
  its file access; the worktree is the boundary instead.

The contract is deliberately narrow, and one rule is load-bearing: **an adapter
reports what it did and never decides whether that was acceptable**
(``docs/020`` §3.1). Gating belongs to the workflow engine. An adapter that
could mark itself green is an adapter with no gate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from orchestrator.agent.memory import TranscriptEntry
from orchestrator.agent.model import AttemptResult, BudgetLedger, TaskSpec
from orchestrator.agent.tools import ToolContext

__all__ = ["AgentPort"]


class AgentPort(Protocol):
    """Performs one attempt at one task."""

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
            spec: What to do, plus any feedback from a failed prior attempt.
            ctx: Carries the workspace root — the attempt's boundary.
            ledger: The budget to spend against, on all three axes.
            transcript_sink: Receives transcript entries as they occur. Called
                per attempt rather than per adapter because one adapter serves
                many concurrent attempts and only the caller knows which run,
                task, and attempt an entry belongs to.

        Returns:
            A terminal result. Implementations must **not** raise for an
            ordinary bad outcome — a refusal, an exhausted budget, a backend
            failure, or a cancellation all come back as results the workflow
            can record.
        """
        ...
