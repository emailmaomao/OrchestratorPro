"""The prompt builder.

Prompt caching is a prefix match: any byte that changes invalidates everything
after it. So a prompt is not assembled in whatever order is convenient — it is
assembled **in order of stability**, most stable first
(``ORCHESTRATOR_PRO_SPEC`` §9)::

    tools             frozen per registry, sorted by name
    system prompt     frozen per role — no timestamps, no identifiers    ◀ cache
    repository context stable per run
    task specification stable per task
    attempt feedback   varies per attempt
    conversation       varies per turn                                   ◀ cache

Two invariants are enforced in code rather than left to reviewer memory, because
both fail silently — a broken cache costs money without producing a single error:

* **No volatile content in the stable prefix.** :func:`check_stability` rejects
  ISO-8601 timestamps and identifier-shaped strings in role prompts. Every one
  of those has been someone's cache bug.
* **Deterministic rendering.** :meth:`PromptBuilder.fingerprint` hashes the
  stable prefix, so a test can assert that two renders of identical inputs
  produce identical bytes.

The builder is provider-agnostic. It emits a neutral
:class:`~orchestrator.provider.base.CompletionRequest`; which backend serves it,
and how that backend spells caching, is the provider's business.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from orchestrator.core.config import Effort, ThinkingMode
from orchestrator.core.events import OrchestratorError
from orchestrator.provider.base import (
    CacheHint,
    CompletionRequest,
    Message,
    TextBlock,
    ToolSpec,
    estimate_tokens,
)
from orchestrator.agent.model import AgentRole, TaskSpec

__all__ = [
    "DEFAULT_ROLE_PROMPTS",
    "PromptBuilder",
    "PromptStabilityError",
    "RepositoryContext",
    "check_stability",
]

#: An ISO-8601 timestamp anywhere in a frozen prompt is a cache bug.
_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

#: Our own identifiers — ``run_01J8...`` — likewise.
_IDENTIFIER: Final = re.compile(r"\b(run|task|att|evt)_[0-9A-HJKMNP-TV-Z]{26}\b")

#: A bare UUID is the third common way volatile data reaches a system prompt.
_UUID: Final = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


class PromptStabilityError(OrchestratorError):
    """Volatile content was found in a part of the prompt that must be frozen."""

    code = "prompt_stability"
    retryable = False


def check_stability(text: str, *, where: str = "system prompt") -> str:
    """Reject volatile content in a prompt section that must stay byte-stable.

    Args:
        text: The candidate text.
        where: What is being checked, for the error message.

    Returns:
        The text, unchanged, when it is safe.

    Raises:
        PromptStabilityError: If a timestamp, identifier, or UUID is present.
    """
    for pattern, label in (
        (_TIMESTAMP, "a timestamp"),
        (_IDENTIFIER, "an identifier"),
        (_UUID, "a UUID"),
    ):
        found = pattern.search(text)
        if found is not None:
            raise PromptStabilityError(
                f"{where} contains {label} ({found.group(0)!r}). Anything that "
                "varies between requests invalidates the prompt cache for every "
                "byte after it — move it into the conversation instead.",
                detail={"where": where, "match": found.group(0)},
            )
    return text


#: Role prompts. Frozen strings: no interpolation, no formatting, no clock.
DEFAULT_ROLE_PROMPTS: Final[Mapping[AgentRole, str]] = {
    AgentRole.WORKER: (
        "You are a software engineer working on one well-scoped task inside an "
        "isolated checkout of a repository.\n\n"
        "Work by using the tools available to you. Read before you write; make "
        "the smallest change that accomplishes the task; match the surrounding "
        "code's conventions rather than importing your own.\n\n"
        "Do not widen the task. If you believe the task is mistaken, say so in "
        "one sentence and complete it as specified anyway.\n\n"
        "When the work is done, call the finish tool with a short summary. Do "
        "not claim completion for work you have not actually done."
    ),
    AgentRole.PLANNER: (
        "You decompose a software change into a graph of independently "
        "executable tasks.\n\n"
        "Each task must be small enough for one engineer to complete without "
        "further decomposition, and must state its own acceptance condition. "
        "Declare a dependency only where one genuinely exists — a false "
        "dependency serializes work that could have run in parallel.\n\n"
        "Prefer fewer, well-specified tasks over many vague ones."
    ),
    AgentRole.REVIEWER: (
        "You review a proposed change for correctness.\n\n"
        "Report every issue you find, including ones you are uncertain about. "
        "Do not filter for importance — a separate step does that. For each "
        "finding give the location, what breaks, and how you know.\n\n"
        "Say plainly when you find nothing wrong."
    ),
    AgentRole.SUMMARIZER: (
        "You summarize what an engineering attempt did.\n\n"
        "Lead with the outcome. State what changed and what did not. Be "
        "specific about anything that was left incomplete — a summary that "
        "omits an unfinished part is worse than no summary."
    ),
}


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    """Facts about the repository that stay constant for a whole run.

    Placed after the system prompt and before anything task-specific, so it is
    cached once per run rather than re-sent per attempt.
    """

    summary: str = ""
    conventions: str = ""
    file_tree: str = ""

    def render(self) -> str:
        """Render as one block, or ``""`` when empty."""
        sections: list[str] = []
        if self.summary:
            sections.append(f"# Repository\n{self.summary.strip()}")
        if self.conventions:
            sections.append(f"# Conventions\n{self.conventions.strip()}")
        if self.file_tree:
            sections.append(f"# Layout\n{self.file_tree.strip()}")
        return "\n\n".join(sections)

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to render."""
        return not (self.summary or self.conventions or self.file_tree)


class PromptBuilder:
    """Assembles neutral completion requests in stability order."""

    __slots__ = ("_context", "_role_prompts")

    def __init__(
        self,
        *,
        role_prompts: Mapping[AgentRole, str] | None = None,
        context: RepositoryContext | None = None,
    ) -> None:
        """Create the builder.

        Args:
            role_prompts: Frozen prompt per role. Each is checked for volatile
                content at construction, so a cache bug fails here rather than
                showing up later as an unexplained cost increase.
            context: Repository facts, constant for the run.

        Raises:
            PromptStabilityError: If any role prompt contains volatile content.
        """
        prompts = dict(role_prompts or DEFAULT_ROLE_PROMPTS)
        for role, text in prompts.items():
            check_stability(text, where=f"the {role.value} system prompt")
        self._role_prompts = prompts
        self._context = context or RepositoryContext()

    @property
    def context(self) -> RepositoryContext:
        """The repository context."""
        return self._context

    def role_prompt(self, role: AgentRole) -> str:
        """Return the frozen system prompt for a role.

        Raises:
            PromptStabilityError: If the role has no prompt. Falling back to a
                generic one would silently change how an agent behaves.
        """
        try:
            return self._role_prompts[role]
        except KeyError:
            raise PromptStabilityError(
                f"no system prompt is defined for the {role.value!r} role",
                detail={"role": role.value, "known": sorted(r.value for r in self._role_prompts)},
            ) from None

    def system_blocks(self, spec: TaskSpec) -> tuple[TextBlock, ...]:
        """Build the stable prefix: role prompt, repository context, task.

        Ordered most-stable first. The task specification comes last because it
        is the first thing that varies — per task rather than per run.
        """
        blocks = [TextBlock(self.role_prompt(spec.role))]
        if not self._context.is_empty:
            blocks.append(TextBlock(self._context.render()))
        blocks.append(TextBlock(self._render_task(spec)))
        return tuple(blocks)

    @staticmethod
    def _render_task(spec: TaskSpec) -> str:
        """Render the task specification.

        The task identifier is deliberately absent: it varies per task and would
        bust the run-level cache without telling the model anything it can use.
        """
        sections = [f"# Task\n{spec.title.strip()}", spec.prompt.strip()]
        if spec.labels:
            sections.append("Labels: " + ", ".join(sorted(spec.labels)))
        return "\n\n".join(sections)

    @staticmethod
    def _render_feedback(spec: TaskSpec) -> str:
        """Render prior-attempt feedback (FR-2.5).

        Kept out of the stable prefix on purpose: feedback changes per attempt,
        so putting it earlier would invalidate the cache on every retry.
        """
        if not spec.feedback:
            return ""
        lines = [
            "# Previous attempts",
            "Earlier attempts at this task failed for the reasons below. Read "
            "them before starting; do not repeat the same approach.",
        ]
        lines.extend(
            f"{index}. {note.strip()}" for index, note in enumerate(spec.feedback, start=1)
        )
        return "\n\n".join(lines)

    def opening_messages(self, spec: TaskSpec) -> tuple[Message, ...]:
        """Return the turns that open a conversation for this task."""
        feedback = self._render_feedback(spec)
        if feedback:
            return (Message.user(feedback),)
        return (Message.user("Begin the task described in the system prompt."),)

    def build_request(
        self,
        *,
        model: str,
        spec: TaskSpec,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_output_tokens: int = 16_000,
        effort: Effort = Effort.HIGH,
        reasoning: ThinkingMode = ThinkingMode.ADAPTIVE,
        cache: bool = True,
    ) -> CompletionRequest:
        """Assemble a neutral completion request.

        Args:
            model: The model identifier, opaque here.
            spec: The task being worked on.
            messages: The conversation so far.
            tools: Tool specifications, **already sorted by the registry**.
            max_output_tokens: Ceiling on the reply.
            effort: Neutral depth intent.
            reasoning: Neutral reasoning intent.
            cache: Whether to request prefix caching.

        Returns:
            The request, ready for any provider.
        """
        return CompletionRequest(
            model=model,
            system=self.system_blocks(spec),
            messages=tuple(messages),
            tools=tuple(tools),
            max_output_tokens=max_output_tokens,
            effort=effort,
            reasoning=reasoning,
            cache_hint=CacheHint() if cache else None,
        )

    def overhead_tokens(self, spec: TaskSpec, tools: Sequence[ToolSpec] = ()) -> int:
        """Estimate the tokens the stable prefix and tools consume.

        The context manager needs this before a request exists, to know how much
        of the window is left for conversation.
        """
        system = sum(estimate_tokens(block.text) for block in self.system_blocks(spec))
        tool_tokens = sum(
            estimate_tokens(tool.name)
            + estimate_tokens(tool.description)
            + estimate_tokens(repr(dict(tool.schema)))
            for tool in tools
        )
        return system + tool_tokens

    def fingerprint(self, spec: TaskSpec, tools: Sequence[ToolSpec] = ()) -> str:
        """Hash the cacheable prefix.

        Two renders of identical inputs must produce the same fingerprint. When
        one does not, the prefix is not stable and prompt caching is silently
        not happening.
        """
        digest = hashlib.sha256()
        for tool in tools:
            digest.update(tool.name.encode("utf-8"))
            digest.update(tool.description.encode("utf-8"))
            digest.update(repr(sorted(dict(tool.schema).items())).encode("utf-8"))
        for block in self.system_blocks(spec):
            digest.update(block.text.encode("utf-8"))
        return digest.hexdigest()
