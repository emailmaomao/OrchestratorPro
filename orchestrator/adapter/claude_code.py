"""Claude Code as the agent harness, not as a model.

The other mapping of the same CLI. ``provider/claude_cli.py`` treats ``claude``
as a one-shot text generator behind ``ModelPort``; this module hands it the
whole attempt: it runs **inside the attempt's worktree**, edits files with its
own tools, and reports what changed. The engine keeps everything that makes an
attempt trustworthy — isolation (the worktree), verification (the project's own
gate), the commit, the merge, the budget, the transcript — and delegates only
the editing.

**This is the ``EXTERNAL_IO`` case ``docs/030`` §5.2 A-4 warns about**, and the
warning is accepted deliberately rather than overlooked. A harness that opens
files itself cannot be confined by our ``FilesystemPort``; §5.2 says such a
harness should run only behind a container-backed shell. It does not here, and
the reasoning is:

* the **worktree is the blast radius** — a throwaway checkout the engine
  created, never the operator's tree, and nothing merges to a default branch;
* the CLI runs under the operator's own installation and login, on their own
  machine, with the permissions they already granted it;
* the gate still decides. The harness cannot mark itself green.

What is genuinely given up: per-tool audit. A tool-loop attempt records every
call; this records the CLI's narration and the resulting diff. That is a real
loss of resolution, stated rather than glossed.

**Shape, not inheritance.** This satisfies the same call the executor makes on
:class:`~orchestrator.agent.runtime.AgentRuntime` —
``run(spec, ctx, ledger, transcript_sink=...) -> AttemptResult`` — which is the
``Adapter`` seam ``docs/030`` §5.2 describes and ``orchestrator/adapter/`` has
held open since v1.0. No protocol is imported because none is declared yet; the
signature is the contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.agent.memory import TranscriptEntry, TranscriptKind
from orchestrator.agent.model import (
    AttemptResult,
    AttemptStatus,
    BudgetLedger,
    TaskSpec,
    TokenUsage,
)
from orchestrator.agent.tools import ToolContext
from orchestrator.core.events import BudgetAxis, BudgetExhaustedError
from orchestrator.provider.base import estimate_tokens
from orchestrator.provider.claude_cli import (
    CliResult,
    CliRunner,
    SubprocessCliRunner,
    parse_envelope,
    resolve_executable,
)

__all__ = ["ClaudeCodeHarness", "HarnessConfig"]

#: Tools the harness is allowed to use. Read/Edit/Write/Grep/Glob cover editing
#: a checkout; Bash is **excluded** deliberately — the engine's own gate runs
#: the tests, and an agent that can shell out inside the worktree is a wider
#: surface than this task needs.
_DEFAULT_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Grep", "Glob")

#: stderr fragments meaning the CLI's login is missing or expired.
_AUTH_MARKERS = ("oauth", "authenticat", "login", "unauthorized")


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """How the harness invokes the CLI.

    Attributes:
        executable: The CLI to run. A bare name resolves on PATH.
        timeout_s: Wall-clock ceiling for one attempt.
        allowed_tools: Tools the CLI may use inside the worktree.
        permission_mode: ``acceptEdits`` lets it write without prompting, which
            is required headlessly — an interactive prompt would simply hang
            until the timeout.
        model: Passed as ``--model`` only when non-empty; empty means the CLI's
            own configured default decides.

            Defaults to ``sonnet`` as of 2026-07-28. Before that it was empty,
            which sounds neutral and is not: it silently inherited whatever the
            operator's CLI happened to be set to — observed as
            ``claude-fable-5`` in run 5 — so the engine's behaviour depended on
            a setting outside it that nothing recorded. A named default is a
            decision; an inherited one is an accident that happens to work.

            An **alias**, not a pinned identifier: under a subscription the CLI
            resolves ``sonnet`` to whatever Sonnet it currently serves, and
            pinning a dated id here would go stale without saying so. Set it to
            ``opus`` for a genuinely hard step; set it to ``""`` to restore the
            inherit-from-CLI behaviour.
    """

    executable: str = "claude"
    timeout_s: float = 1800.0
    allowed_tools: tuple[str, ...] = _DEFAULT_TOOLS
    permission_mode: str = "acceptEdits"
    model: str = "sonnet"


@dataclass(frozen=True, slots=True)
class _Invocation:
    """One CLI run, for the transcript."""

    argv: Sequence[str]
    result: CliResult
    changed: tuple[str, ...] = field(default_factory=tuple)


class ClaudeCodeHarness:
    """Runs one attempt by handing the worktree to Claude Code."""

    __slots__ = ("_config", "_list_changed", "_runner")

    def __init__(
        self,
        config: HarnessConfig | None = None,
        *,
        runner: CliRunner | None = None,
        list_changed: Callable[[Path], Sequence[str]] | None = None,
    ) -> None:
        """Create the harness.

        Args:
            config: Invocation settings.
            runner: Executes the CLI. Tests inject a scripted one, which is how
                this is exercised without a subscription.
            list_changed: Reports which paths changed in a directory. Defaults
                to asking Git inside the worktree.
        """
        self._config = config or HarnessConfig()
        self._runner: CliRunner = runner or SubprocessCliRunner()
        self._list_changed = list_changed or _git_changed_files

    @property
    def config(self) -> HarnessConfig:
        """The harness's settings."""
        return self._config

    def build_argv(self, prompt: str, worktree: Path | None = None) -> list[str]:
        """Translate an attempt's prompt into the CLI invocation.

        Two resolutions happen here, both learned the hard way. The executable
        is resolved past the npm ``.cmd`` shim, because a shim routes
        arguments through ``cmd.exe`` and silently truncates each one at its
        first newline — a multi-line prompt arrives as its opening line. And
        the worktree is granted explicitly with ``--add-dir``: the narrowest
        grant that works, rather than ``--dangerously-skip-permissions``,
        which would drop every check for the whole process instead of scoping
        one directory.
        """
        argv = [
            resolve_executable(self._config.executable),
            "-p",
            prompt,
            # JSON, not text (OP-014). The envelope carries what the attempt
            # actually consumed and — as importantly — which tools it was
            # refused. Text mode gave neither, which is why every token this
            # harness reported was a guess and why a permission-blocked
            # attempt took two runs and an argv-dumping shim to diagnose.
            "--output-format",
            "json",
            "--permission-mode",
            self._config.permission_mode,
        ]
        if worktree is not None:
            argv += ["--add-dir", str(worktree)]
        if self._config.allowed_tools:
            argv += ["--allowedTools", *self._config.allowed_tools]
        if self._config.model:
            argv += ["--model", self._config.model]
        return argv

    def build_prompt(self, spec: TaskSpec) -> str:
        """Assemble the instruction the harness is given.

        Feedback from a failed attempt goes **last**: it is the most recent and
        most specific thing the agent needs to know (FR-2.5).
        """
        parts = [
            "You are working inside a Git worktree that has been prepared for "
            "you. Make the change described below by editing files directly. "
            "Do not commit; the surrounding system commits, runs the project's "
            "test suite as a gate, and merges. Do not run the test suite "
            "yourself.",
            "",
            f"# Task: {spec.title}",
            "",
            spec.prompt,
        ]
        if spec.feedback:
            parts += [
                "",
                "# A previous attempt failed. What went wrong:",
                "",
                *spec.feedback,
            ]
        return "\n".join(parts)

    async def run(
        self,
        spec: TaskSpec,
        ctx: ToolContext,
        ledger: BudgetLedger,
        *,
        transcript_sink: Callable[[TranscriptEntry], None] | None = None,
    ) -> AttemptResult:
        """Perform one attempt by delegating the editing to the CLI.

        Matches :meth:`AgentRuntime.run` so the executor can hold either.
        Never raises for an ordinary bad outcome; every failure comes back as
        a result the workflow can record.
        """
        root = Path(ctx.workspace_root)
        prompt = self.build_prompt(spec)
        argv = self.build_argv(prompt, root)
        entries: list[TranscriptEntry] = []
        index = 0

        def record(kind: TranscriptKind, content: str, **detail: object) -> None:
            nonlocal index
            entry = TranscriptEntry(
                kind=kind, content=content, index=index, detail=detail
            )
            entries.append(entry)
            index += 1
            if transcript_sink is not None:
                transcript_sink(entry)

        record(TranscriptKind.USER, prompt, harness="claude_code")

        # One tool call in the ledger's terms: the whole delegated attempt.
        # Charging per edit is impossible from outside the CLI, and pretending
        # otherwise would be a fabricated number.
        try:
            ledger.record_tool_call()
            ledger.check()
        except BudgetExhaustedError as exc:
            return AttemptResult(
                status=AttemptStatus.BUDGET_EXHAUSTED,
                summary=str(exc),
                error_code=exc.code,
                detail=dict(exc.detail),
            )

        # Never outlive the attempt's own wall-clock budget: the executor also
        # wraps this in wait_for, and a harness killed from outside leaves an
        # orphaned CLI holding the worktree open.
        timeout = min(
            self._config.timeout_s,
            max(1.0, ledger.remaining(BudgetAxis.SECONDS)),
        )
        try:
            result = await self._runner.run(argv, timeout_s=timeout, cwd=root)
        except FileNotFoundError as exc:
            record(TranscriptKind.NOTE, f"the CLI was not found: {exc}")
            return AttemptResult(
                status=AttemptStatus.ERRORED,
                summary=(
                    f"the Claude Code CLI was not found at "
                    f"{self._config.executable!r}"
                ),
                error_code="unavailable",
            )

        changed = tuple(await asyncio.to_thread(self._list_changed, root))
        envelope = parse_envelope(result.stdout)
        text = (envelope.text if envelope is not None else result.stdout).strip()
        denials = list(envelope.permission_denials) if envelope is not None else []
        record(
            TranscriptKind.ASSISTANT,
            text or "(no output)",
            exit_code=result.exit_code,
            changed_files=list(changed),
            permission_denials=denials,
        )
        if denials:
            # Loud, because this is the run-3 failure mode: an agent refused a
            # tool, changed nothing, exited zero, and the gate then verified an
            # unchanged tree. The no-op guard now fails that attempt — but
            # without this line the *reason* is still nowhere in the record.
            record(
                TranscriptKind.NOTE,
                "the CLI was refused these tools: " + ", ".join(denials),
                permission_denials=denials,
            )

        if envelope is not None and envelope.measured:
            # Measured (OP-014), and labelled as such through the OP-004 path.
            usage = TokenUsage(
                input_tokens=envelope.input_tokens,
                output_tokens=envelope.output_tokens,
                cost_usd=None,  # subscription: no charge to report. See below.
                estimated=False,
            )
        else:
            # No usage block, or an envelope we could not read. Degrade to the
            # old estimate rather than fail an attempt that did the work, and
            # never report absence as zero — zero is a measurement, and false.
            usage = TokenUsage(
                input_tokens=estimate_tokens(prompt),
                output_tokens=estimate_tokens(text),
                cost_usd=None,
                estimated=True,
            )
        ledger.record_tokens(usage.total_tokens, estimated=usage.estimated)

        # The envelope's dollar figure is what the call would have cost at list
        # API prices. Under a subscription the marginal cost was zero, so it
        # travels under its own name and never as `cost_usd`: telling an
        # operator they spent money they did not spend is the $0.00 error
        # inverted, and no less wrong.
        extra: dict[str, object] = {}
        if envelope is not None and envelope.notional_cost_usd is not None:
            extra["notional_cost_usd"] = envelope.notional_cost_usd
        if denials:
            extra["permission_denials"] = denials

        if result.timed_out:
            return AttemptResult(
                status=AttemptStatus.BUDGET_EXHAUSTED,
                changed_files=changed,
                usage=usage,
                summary=(
                    f"the harness exceeded its {timeout:g}s ceiling; partial "
                    "work is preserved in the worktree"
                ),
                error_code="timeout",
                detail={"axis": "seconds", "limit": timeout, **extra},
            )

        if result.exit_code != 0:
            stderr = result.stderr.strip()
            lowered = stderr.lower()
            authentication = any(marker in lowered for marker in _AUTH_MARKERS)
            record(TranscriptKind.NOTE, stderr[:2000] or "(no stderr)")
            return AttemptResult(
                status=AttemptStatus.ERRORED,
                changed_files=changed,
                usage=usage,
                summary=(
                    "the Claude Code CLI is not signed in (or its session "
                    "expired); run `claude` and use /login"
                    if authentication
                    else f"the harness exited {result.exit_code}"
                ),
                error_code="auth_failed" if authentication else "harness_error",
                detail={"stderr": stderr[:2000], **extra},
            )

        return AttemptResult(
            status=AttemptStatus.SUCCEEDED,
            changed_files=changed,
            usage=usage,
            summary=text[:2000] or "the harness reported no output",
            detail={"transcript_entries": len(entries), **extra},
        )


def _git_changed_files(root: Path) -> list[str]:
    """Return paths Git reports as changed inside ``root``.

    Asking Git is the only honest answer: the harness edits files itself, so
    the engine cannot know what it touched from the outside. Failure to ask is
    reported as "nothing known" rather than raising — an attempt that worked
    should not fail because the listing did.
    """
    import subprocess

    try:
        # No S603 waiver needed here: ruff does not flag a call whose argument
        # list is entirely static literals, which is the design rule stated
        # rather than merely asserted.
        completed = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607 - git from PATH, as everywhere
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        entry = line[3:].strip() if len(line) > 3 else ""
        if entry and entry not in paths:
            paths.append(entry)
    return paths
