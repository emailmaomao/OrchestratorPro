"""Claude Code (the ``claude`` CLI) behind the neutral model port.

The subscription-billed backend: the CLI authenticates against the operator's
own Claude subscription, so **no API key exists anywhere** — the guardrail
hard-won production fixes as non-negotiable, and a legitimate backend class in
its own right.

**The design decision, recorded.** ``claude -p`` is an agent harness, not a
bare model — it has its own tools and can edit files in its working directory.
Two honest mappings exist: this one (``ModelPort``: one-shot text in, text
out, no tool calling declared) and the ``agent`` domain of ``docs/030`` §5.2
(the harness doing its own file edits — the ``EXTERNAL_IO`` confinement case).
This module is deliberately the first: it slots into the existing runtime with
zero new protocols, and the first consumer's ``generate`` intent is
text-shaped. The harness mapping is future work with its own confinement
conversation.

**Invocation rules** (each already paid for once, in production
``7f54dde`` — do not relearn them):

=========================  ==============================================
Rule                       Why
=========================  ==============================================
Prompt as ONE ``-p`` arg   the Windows ``claude.cmd`` shim swallows piped
                           stdin and hangs
``--output-format json``   the envelope carries MEASURED usage; text mode
                           gave nothing to count, so every token this
                           backend reported used to be a guess (OP-014).
                           Unreadable envelope degrades to text + estimates
never a shell string       argv only; the injection surface stays closed
kill the process group     an orphaned ``node`` child under ``claude.cmd``
on timeout                 survives killing the shim alone
=========================  ==============================================

**Capability honesty.** ``USAGE_REPORTING`` is declared — the JSON envelope
reports what the call actually consumed. ``TOKEN_COUNTING`` is **not**, and
the distinction matters: that one promises ``count_tokens`` is exact, a
*pre-flight* answer given without running anything, which the CLI cannot do at
any price. Measured usage arriving after a call is a different guarantee.

Still undeclared: streaming, tool calling (in this mapping), cost reporting,
prompt caching, refusal semantics — asking for one raises ``NOT_SUPPORTED``
instead of quietly emulating it. ``cost_usd`` stays ``None``: the envelope's
dollar figure is a list-price equivalent for a call the subscription already
covered, surfaced as ``notional_cost_usd`` so nothing renders it as a charge.

**Model selection.** ``--model`` is never passed unless
:attr:`ClaudeCliConfig.model` is explicitly set: under a subscription the
CLI's own configured default is authoritative, and the registry factory leaves
it empty rather than forwarding the API-provider default (which would silently
override the operator's CLI settings).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from orchestrator.provider.base import (
    CapabilitySet,
    CompletionRequest,
    CompletionResponse,
    Domain,
    ErrorCode,
    Feature,
    HealthReport,
    ProviderContext,
    ProviderError,
    StopReason,
    StreamEvent,
    TextBlock,
    Usage,
    estimate_tokens,
    request_text,
)

__all__ = [
    "ClaudeCliConfig",
    "ClaudeCliProvider",
    "CliEnvelope",
    "CliResult",
    "CliRunner",
    "SubprocessCliRunner",
    "parse_envelope",
    "resolve_executable",
]

#: Names the CLI ships under, most specific first. npm installs it on Windows
#: as ``claude.CMD`` beside an extensionless shell script; ``CreateProcess``
#: searches PATH but only appends ``.exe``, so ``Popen(["claude", ...])`` fails
#: with "not found" on a machine where ``claude`` works perfectly in a shell.
#: A downstream project learned this and encoded the same candidate list; this
#: module cited the lesson in its docstring and then did not apply it — the
#: first real harness run failed on exactly it.
_EXECUTABLE_CANDIDATES = ("", ".cmd", ".exe", ".bat", ".ps1")

#: Batch shims route their arguments through ``cmd.exe`` as ``%*``, which
#: **truncates any argument at its first newline, silently**. A multi-line
#: prompt therefore arrives as its first line and nothing else — the agent
#: receives a preamble with no task and reasonably reports that no
#: instruction was given. This is the sibling of the stdin-swallow bug
#: A downstream project hit: the same shim, the other channel. Resolving past
#: the shim to the real binary is the only fix that keeps the prompt intact.
_SHIM_SUFFIXES = (".cmd", ".bat")

#: A quoted absolute path to a real executable inside an npm shim. Matching is
#: deliberately narrow — a real path that exists on disk, or nothing.
_SHIM_TARGET = re.compile(r'"([^"\n]+?\.exe)"', re.IGNORECASE)


def resolve_executable(name: str) -> str:
    """Return a launchable path for ``name``, or ``name`` unchanged.

    Two problems, one helper. First, a bare name may not be launchable at all:
    ``CreateProcess`` appends only ``.exe``, so the ``.cmd`` npm installs is
    invisible to it. Second — and worse, because it fails *silently* — a
    ``.cmd`` shim passes arguments through ``cmd.exe``, which truncates each
    one at its first newline. A multi-line prompt loses everything after line
    one and the agent is left guessing.

    So a resolved shim is followed to the executable it wraps. If that cannot
    be determined the shim is used anyway: a truncated prompt is bad, but
    refusing to run at all when the CLI is demonstrably installed is worse,
    and the caller sees the CLI's own behaviour either way.

    Returning the input untouched when nothing resolves is deliberate: the
    caller's own "not found" handling then produces the error, with its own
    message and error code, rather than this helper inventing one.
    """
    resolved = shutil.which(name)
    if resolved is None:
        for suffix in _EXECUTABLE_CANDIDATES:
            if not suffix:
                continue
            found = shutil.which(f"{name}{suffix}")
            if found:
                resolved = found
                break
    if resolved is None:
        return name
    return _resolve_shim(resolved)


def _resolve_shim(path: str) -> str:
    """Follow a batch shim to the executable it wraps, if it is one."""
    if not path.lower().endswith(_SHIM_SUFFIXES):
        return path
    try:
        script = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path
    for candidate in _SHIM_TARGET.findall(script):
        # npm writes the target relative to %dp0%, the shim's own directory.
        expanded = candidate.replace("%dp0%", str(Path(path).parent)).replace(
            "%~dp0", str(Path(path).parent)
        )
        target = Path(expanded.replace("\\\\", "\\"))
        if target.is_file():
            return str(target)
    return path


@dataclass(frozen=True, slots=True)
class CliEnvelope:
    """What ``--output-format json`` reported about one invocation.

    The CLI's JSON mode returns a single object wrapping the answer with real
    accounting. Reading it is the whole of OP-014: before this, every token
    recorded from a CLI-backed attempt was a four-characters-per-token guess,
    not because the CLI could not say but because we asked for plain text.

    Attributes:
        text: The assistant's answer — the ``result`` field, which is exactly
            what ``--output-format text`` would have printed.
        ok: Whether the envelope claims success. Independent of the exit code
            and more specific than it.
        input_tokens: **Everything the model read**: fresh input plus cache
            creation plus cache reads. The CLI's own ``input_tokens`` counts
            only the uncached remainder — 2 against 26,539 in the probe that
            motivated this — and a budget built on that number would never
            bind. The breakdown survives in :attr:`cached_input_tokens` so the
            cache saving stays visible.
        output_tokens: Measured output.
        cached_input_tokens: The part of the input served from or written to
            cache.
        notional_cost_usd: ``total_cost_usd``. **Not a charge** — see
            :class:`~orchestrator.provider.base.Usage`.
        permission_denials: Tools the CLI was refused. Empty in the ordinary
            case; when it is not, it is usually the whole explanation for an
            attempt that reported success and changed nothing.
        measured: False when no usage block could be read, in which case the
            counts here are zero and the caller must fall back to estimating.
            Absence is never reported as zero consumption.
    """

    text: str
    ok: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    notional_cost_usd: float | None = None
    permission_denials: tuple[str, ...] = ()
    measured: bool = False


def parse_envelope(stdout: str) -> CliEnvelope | None:
    """Read a ``--output-format json`` envelope, or ``None`` if it is not one.

    Returns ``None`` rather than raising for anything unreadable — a truncated
    write, a future schema, a version that ignored the flag. The caller then
    degrades to treating the output as text and estimating, which is exactly
    what it did before this function existed. **An attempt that did the work
    must never fail over a reporting detail.**
    """
    raw = stdout.strip()
    if not raw.startswith("{"):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    text = parsed.get("result")
    if not isinstance(text, str):
        # No answer field means this is not the envelope we know how to read.
        return None

    denials = parsed.get("permission_denials")
    denied = (
        tuple(_denial_name(item) for item in denials)
        if isinstance(denials, list)
        else ()
    )
    envelope = CliEnvelope(
        text=text,
        ok=not bool(parsed.get("is_error", False)),
        notional_cost_usd=_as_optional_float(parsed.get("total_cost_usd")),
        permission_denials=denied,
    )

    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        # A well-formed answer with no usage block: keep the text, keep the
        # denials, and say plainly that nothing was measured. Reporting zero
        # tokens would be a measurement of "it consumed nothing", which is
        # false about every call that produced an answer.
        return envelope

    fresh = _as_count(usage.get("input_tokens"))
    created = _as_count(usage.get("cache_creation_input_tokens"))
    read = _as_count(usage.get("cache_read_input_tokens"))
    return replace(
        envelope,
        input_tokens=fresh + created + read,
        output_tokens=_as_count(usage.get("output_tokens")),
        cached_input_tokens=created + read,
        measured=True,
    )


def _denial_name(item: object) -> str:
    """Render one permission denial as something a person can read."""
    if isinstance(item, dict):
        for key in ("tool_name", "tool", "name"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return str(item)


def _as_count(value: object) -> int:
    """Read a non-negative integer from a payload, or zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _as_optional_float(value: object) -> float | None:
    """Read an optional float from a payload."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


#: stderr fragments that mean the CLI's login is missing or expired. Matched
#: case-insensitively. An expired session is not transient: retrying burns
#: wall-clock to produce the identical failure, so it maps to AUTH_FAILED.
_AUTH_MARKERS = ("oauth", "authenticat", "login", "unauthorized", "api key")

#: Grace between asking a process group to stop and killing it outright.
_TERMINATE_GRACE_S = 2.0


@dataclass(frozen=True, slots=True)
class CliResult:
    """What one CLI invocation produced.

    A timeout is reported on the result, not raised — the same convention as
    the test runner's ``ProcessResult``, and for the same reason: overrunning
    is an outcome to classify, not an exception to unwind through.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CliRunner(Protocol):
    """Executes one CLI invocation. The seam the offline suite scripts.

    May raise :class:`FileNotFoundError` when the executable does not exist;
    every other failure is reported on the :class:`CliResult`.
    """

    async def run(
        self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
    ) -> CliResult:
        """Run ``argv`` and return what happened.

        ``cwd`` is what lets the same runner serve the one-shot provider (which
        does not care where it runs) and the harness adapter (for which the
        working directory *is* the attempt's worktree, and therefore the whole
        blast radius).
        """
        ...


def _group_kwargs() -> dict[str, Any]:
    """Popen keywords that put the child in its own process group.

    Mirrors ``test_runner/execution.py`` — the technique is proven there, but
    the provider layer sits below it and may not import it (NFR-5.3), so the
    forty lines are duplicated knowingly rather than the layering broken.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    # mypy narrows sys.platform to the checking machine's value, so on Windows
    # it proves this line unreachable; on POSIX it is the whole point.
    return {"start_new_session": True}  # type: ignore[unreachable]


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Terminate a process and everything it spawned.

    Best effort by necessity, and it must never raise — it runs on the timeout
    path, where something has already gone wrong. On Windows ``taskkill /T``
    walks the child tree, which matters here: ``claude.cmd`` is a shim whose
    real work happens in a ``node`` child that would survive killing the shim.
    """
    if process.poll() is not None:
        return

    if sys.platform == "win32":
        subprocess.run(  # noqa: S603 - argv only, never a shell
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607 - system tool
            capture_output=True,
            check=False,
        )
    else:
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=_TERMINATE_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):  # nothing left to do
        process.wait(timeout=_TERMINATE_GRACE_S)


def _run_blocking(
    argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
) -> CliResult:
    """Run the CLI synchronously; called through ``asyncio.to_thread``."""
    process = subprocess.Popen(  # noqa: S603 - argv only, never a shell string
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_group_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        # Collect whatever arrived before the kill; communicate() after a
        # TimeoutExpired returns promptly once the process is dead.
        stdout, stderr = process.communicate()
        return CliResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )
    return CliResult(exit_code=process.returncode, stdout=stdout, stderr=stderr)


class SubprocessCliRunner:
    """The real runner: a subprocess in its own process group, off the loop."""

    __slots__ = ()

    async def run(
        self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
    ) -> CliResult:
        """Run ``argv``, killing its whole process group if it overruns."""
        return await asyncio.to_thread(
            _run_blocking, argv, timeout_s=timeout_s, cwd=cwd
        )


@dataclass(frozen=True, slots=True)
class ClaudeCliConfig:
    """Settings for the CLI-backed provider.

    Attributes:
        executable: The CLI to invoke. A bare name resolves on PATH.
        timeout_s: Wall-clock ceiling per invocation.
        model: Passed as ``--model`` **only when non-empty**. Empty means the
            CLI's own configured default decides — the right default under a
            subscription, where the operator manages models in the CLI itself.
    """

    executable: str = "claude"
    timeout_s: float = 900.0
    model: str = ""

    def __post_init__(self) -> None:
        """Refuse configuration that could not possibly run."""
        if not self.executable:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                "claude_cli.executable must not be empty",
                provider_id="model.claude_cli",
                domain=Domain.MODEL,
            )
        if self.timeout_s <= 0:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                f"claude_cli.timeout_s must be positive, got {self.timeout_s}",
                provider_id="model.claude_cli",
                domain=Domain.MODEL,
            )


class ClaudeCliProvider:
    """Claude Code, one-shot, through the neutral model port."""

    id = "model.claude_cli"
    domain = Domain.MODEL
    version = "1.0"

    __slots__ = ("_config", "_runner", "_started", "_version")

    def __init__(
        self,
        config: ClaudeCliConfig | None = None,
        *,
        runner: CliRunner | None = None,
    ) -> None:
        """Construct the provider.

        Args:
            config: Executable, timeout, and optional model override.
            runner: Executes invocations. Tests inject a scripted one, which
                is how the whole provider is exercised with no CLI installed.
        """
        self._config = config or ClaudeCliConfig()
        self._runner: CliRunner = runner or SubprocessCliRunner()
        self._started = False
        self._version = ""

    @property
    def config(self) -> ClaudeCliConfig:
        """The provider's settings."""
        return self._config

    def capabilities(self) -> CapabilitySet:
        """Declare what a one-shot CLI genuinely offers: usage, and little else.

        ``USAGE_REPORTING`` **is** declared as of OP-014: the JSON envelope
        carries what the call actually consumed, so the counts on a response
        are measurements and are labelled as such.

        ``TOKEN_COUNTING`` is **not**, and the difference is the point. That
        capability promises ``count_tokens`` is exact — a *pre-flight* answer,
        given without running anything — and the CLI offers no such mode at
        any price. Declaring it because measured usage arrived would be a
        promise about a different method entirely; a caller sizing a prompt
        against a context window would get an estimate wearing a guarantee.

        Still undeclared and still honest: ``STREAMING``, ``TOOL_CALLING``,
        ``COST_REPORTING``, ``PROMPT_CACHING``, ``REFUSAL_SEMANTICS``. Each
        would be an emulation that behaves differently under load, which is
        the silent degradation ``docs/030`` §4 exists to refuse. Cost in
        particular: the envelope reports a dollar figure, but it is a list-price
        equivalent for a call the subscription already paid for — reported as
        ``notional_cost_usd``, never as ``cost_usd``.
        """
        return CapabilitySet(
            features=frozenset({Feature.USAGE_REPORTING}), limits={}, variants={}
        )

    async def startup(self, ctx: ProviderContext | None = None) -> None:
        """Probe ``--version`` — cheap, auth-free, and fails fast.

        Raises:
            ProviderError: ``UNAVAILABLE`` when the executable is missing or
                the probe fails. At startup, where it costs a second — not at
                the first attempt, where it costs a task.
        """
        argv = [resolve_executable(self._config.executable), "--version"]
        try:
            result = await self._runner.run(argv, timeout_s=30.0)
        except FileNotFoundError as exc:
            raise ProviderError(
                ErrorCode.UNAVAILABLE,
                f"the Claude Code CLI was not found at "
                f"{self._config.executable!r}; install it with "
                "`npm install -g @anthropic-ai/claude-code` and sign in once "
                "with `claude` -> /login",
                provider_id=self.id,
                domain=self.domain,
                cause=str(exc),
            ) from exc
        if result.timed_out or result.exit_code != 0:
            raise ProviderError(
                ErrorCode.UNAVAILABLE,
                f"`{self._config.executable} --version` "
                f"{'timed out' if result.timed_out else f'exited {result.exit_code}'}",
                provider_id=self.id,
                domain=self.domain,
                cause=result.stderr.strip()[:500] or None,
            )
        self._version = result.stdout.strip()
        self._started = True

    async def health(self) -> HealthReport:
        """Report whether the CLI answered its version probe."""
        return HealthReport(
            provider_id=self.id,
            healthy=self._started,
            detail=self._version or "not started",
        )

    async def shutdown(self) -> None:
        """Forget the probe. Idempotent; there is no connection to close."""
        self._started = False
        self._version = ""

    # ------------------------------------------------------------ invocation

    def build_argv(self, request: CompletionRequest) -> list[str]:
        """Translate a neutral request into the CLI invocation.

        Public for the same reason the Anthropic provider's ``build_payload``
        is: the translation is the most valuable thing to test, and it needs
        no process to be observable.
        """
        argv = [
            resolve_executable(self._config.executable),
            "-p",
            request_text(request),
            # JSON, not text: the envelope carries measured usage. Text mode
            # gave us nothing to count, which is why every token this backend
            # ever reported was a guess (OP-014).
            "--output-format",
            "json",
        ]
        if self._config.model:
            argv += ["--model", self._config.model]
        return argv

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one one-shot generation and return the text."""
        argv = self.build_argv(request)
        try:
            result = await self._runner.run(argv, timeout_s=self._config.timeout_s)
        except FileNotFoundError as exc:
            raise ProviderError(
                ErrorCode.UNAVAILABLE,
                f"the Claude Code CLI disappeared from {self._config.executable!r}",
                provider_id=self.id,
                domain=self.domain,
                cause=str(exc),
            ) from exc

        if result.timed_out:
            raise ProviderError(
                ErrorCode.TIMEOUT,
                f"the CLI exceeded its {self._config.timeout_s:g}s ceiling and "
                "its process group was killed",
                provider_id=self.id,
                domain=self.domain,
            )
        if result.exit_code != 0:
            stderr = result.stderr.strip()
            lowered = stderr.lower()
            if any(marker in lowered for marker in _AUTH_MARKERS):
                raise ProviderError(
                    ErrorCode.AUTH_FAILED,
                    "the Claude Code CLI is not signed in (or its session "
                    "expired); run `claude` and use /login, then retry the run",
                    provider_id=self.id,
                    domain=self.domain,
                    cause=stderr[:500] or None,
                )
            raise ProviderError(
                ErrorCode.INTERNAL,
                f"the CLI exited {result.exit_code}",
                provider_id=self.id,
                domain=self.domain,
                cause=stderr[:500] or None,
            )

        envelope = parse_envelope(result.stdout)
        if envelope is not None and envelope.measured:
            text = envelope.text.rstrip("\n")
            usage = Usage(
                input_tokens=envelope.input_tokens,
                output_tokens=envelope.output_tokens,
                cached_input_tokens=envelope.cached_input_tokens,
                # Still None, deliberately. The envelope's total_cost_usd is
                # what this call would have cost at list API prices; under a
                # subscription the marginal cost was nothing. Putting it here
                # would say the operator spent money they did not spend — the
                # $0.00 error inverted, and no less wrong (M-7).
                cost_usd=None,
                tokens_estimated=False,
                notional_cost_usd=envelope.notional_cost_usd,
            )
        else:
            # Degrade visibly rather than fail: an answer arrived, and a
            # missing or unreadable usage block is a reporting detail, not a
            # failed generation. Absence is reported as "estimated", never as
            # zero consumption — zero would be a measurement, and a false one.
            text = (envelope.text if envelope is not None else result.stdout).rstrip("\n")
            usage = Usage(
                input_tokens=estimate_tokens(request_text(request)),
                output_tokens=estimate_tokens(text),
                cost_usd=None,
                tokens_estimated=True,
            )
        return CompletionResponse(
            content=(TextBlock(text),),
            stop_reason=StopReason.END,
            refusal=None,
            usage=usage,
            model_served=self._config.model or "claude-cli",
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """Refuse: the CLI's text mode yields one answer, not increments.

        Emitting the final text as a single fake chunk would "work" until a
        caller relied on incremental arrival — streaming is not declared, so
        asking for it fails loudly (``docs/030`` §4).
        """
        raise ProviderError(
            ErrorCode.NOT_SUPPORTED,
            f"{self.id} does not stream; it does not declare STREAMING",
            provider_id=self.id,
            domain=self.domain,
        )
        yield  # type: ignore[unreachable]  # makes this an async generator

    async def count_tokens(self, request: CompletionRequest) -> int:
        """Estimate — TOKEN_COUNTING (an *exact* count) is not declared.

        Unchanged by OP-014, and deliberately so. The envelope measures what a
        call *consumed*; this asks what a prompt *would* consume, before
        spending anything. The CLI has no answer to the second question.
        """
        return estimate_tokens(request_text(request))
