# 030 — The Universal Provider Interface

**Status:** Approved for M0
**Last updated:** 2026-07-31
**Amends:** `ORCHESTRATOR_PRO_SPEC.md` §4.1, `docs/020_ARCHITECTURE.md` §1 (see
§12 — superseded; the amendments proposed there mostly never landed)

---

## 0. The rule this document exists to enforce

> **No module outside a provider implementation may import a vendor SDK, shell
> out to a named binary, or contain a vendor-specific conditional.**

OrchestratorPro must not depend on Claude, Hermes, OpenAI, Ollama, or any other
specific AI — nor on pytest, Git, Chrome, GitHub, or bash. Each is one
replaceable implementation behind a contract the core owns.

If you can grep the codebase for a vendor name and find a hit outside
`orchestrator/provider/`, that is a defect, and there is an automated test
(§10.1) whose job is to fail when it happens.

### 0.1 One interface, or many?

The instruction is a *universal* Provider interface. Taken literally — one
Protocol implemented by both a language model and a headless browser — the
contract would reduce to `do_something(input) -> output`, which forbids nothing
and guarantees nothing. Universality bought that way is a rename, not an
abstraction.

So the design splits in two, and both halves are mandatory:

| Half | What it is | Applies to |
|---|---|---|
| **The substrate** (§2–4) | Identity, lifecycle, capability declaration, health, error taxonomy, cancellation, telemetry | **Every** provider, in every domain, without exception |
| **The ports** (§5) | The typed contract for one kind of capability | One domain each |

Every provider implements the substrate. Every provider also implements exactly
one port. The substrate is what makes them uniformly *manageable*; the port is
what makes them actually *substitutable*. Claiming universality with only the
first is the failure mode this section exists to prevent.

---

## 1. Vocabulary

This document changes the meaning of one existing term. `ORCHESTRATOR_PRO_SPEC.md`
§2 uses **Provider** to mean "LLM vendor client" and **Adapter** to mean "agent
backend". That was too narrow. The generalized vocabulary:

| Term | Meaning |
|---|---|
| **Provider** | Any replaceable implementation of an external capability. An LLM client, a test runner, a shell, a browser, a Git implementation, a forge API client. |
| **Domain** | A category of capability, e.g. `model`, `shell`, `build`, `vcs`. Exactly one port per domain. |
| **Port** | The typed contract for a domain. `ModelPort`, `ShellPort`, and so on. |
| **Capability set** | A provider's runtime declaration of what it can actually do. Distinct from the port, which is what it *could* be asked. |
| **Registry** | Config-driven resolution of `(domain, name) → provider instance`. |
| **Neutral type** | A type owned by OrchestratorPro that crosses a port boundary. Never a vendor type. |
| **Conformance suite** | The shared per-domain test suite every provider in that domain must pass. |

The old term **Adapter** is retired. What it described is now the `agent` domain
(§5.2). Spec §2 and §4.1 need the corresponding edit (§12).

---

## 2. The substrate: what every provider implements

```python
class Provider(Protocol):
    """Implemented by every provider in every domain, without exception."""

    id: ProviderId          # stable, dotted: "model.anthropic", "shell.container"
    domain: Domain          # the port this provider satisfies
    version: str            # implementation version, not vendor version

    def capabilities(self) -> CapabilitySet: ...
    def config_schema(self) -> type[BaseModel]: ...

    async def startup(self, ctx: ProviderContext) -> None: ...
    async def health(self) -> HealthReport: ...
    async def shutdown(self) -> None: ...
```

### 2.1 Universal obligations

Every provider, in every domain, must:

| # | Obligation | Why |
|---|---|---|
| P-1 | Accept and return **only neutral types**. No vendor object crosses the boundary — not in a return value, not in an exception, not in a `dict` field. | A leaked vendor type makes the port a lie: swapping the provider then breaks callers. |
| P-2 | Raise only `ProviderError` subclasses (§3). Vendor exceptions are caught and translated at the boundary. | Callers cannot handle vendor taxonomies they are not allowed to import. |
| P-3 | **Declare capabilities honestly** (§4). Never claim a capability it does not have; never silently no-op one it lacks. | Silent degradation is the single most expensive failure mode in a swappable-backend system. |
| P-4 | Honor cancellation from `ProviderContext` promptly, and leave no orphaned process, worktree, or connection behind. | Cancellation is a first-class operation (FR-2.9), not a best-effort courtesy. |
| P-5 | Be **stateless between calls**, or make its state explicit in `ProviderContext`. No module-level mutable state, no singletons. | Two configured instances of the same provider must not interfere. |
| P-6 | Read credentials only via `SecretsPort` (§5.9) or the environment. Never from a config file, never into a log, never into a prompt. | NFR-3.3, NFR-3.5. |
| P-7 | Emit telemetry through the context's sink — never `print`, never a bare logger. | Cost and latency attribution (FR-5.4) depends on it. |
| P-8 | Be constructible and usable **offline** in a fake variant that passes the same conformance suite. | Every layer above must be testable without network access (NFR-5.4). |
| P-9 | Declare a Pydantic config schema and validate at startup, failing fast on bad config. | `ConfigError` at startup beats a 400 an hour into a run. |
| P-10 | Be **idempotent where the port says so**, and say so where it cannot be. | Crash recovery (`020` §5.1) depends on knowing which operations can be safely repeated. |

### 2.2 `ProviderContext`

The only channel through which a provider receives ambient state. Nothing is
reached through globals.

```python
@dataclass(frozen=True)
class ProviderContext:
    run_id: RunId | None
    task_id: TaskId | None
    attempt_id: AttemptId | None
    workspace_root: Path | None      # confinement boundary, where applicable
    deadline: float | None           # monotonic; provider must respect it
    cancel: CancelToken
    secrets: SecretsPort
    telemetry: TelemetrySink
    logger: BoundLogger
```

`workspace_root` is the containment boundary for every filesystem-touching
provider. A provider given a `workspace_root` must confine all I/O to it (§5.5),
and this is verified by the conformance suite, not by trust.

### 2.3 Lifecycle

```
  construct(config)  ──▶  startup(ctx)  ──▶  [ health() ]*  ──▶  shutdown()
        │                      │                                     │
        │ ConfigError          │ ProviderUnavailable                 │ always runs,
        │ (fail fast)          │ (fail fast)                         │ even on error
```

`startup` is where a provider verifies it can actually work: binary present,
credential resolvable, endpoint reachable, model id valid. Discovering at token
40 000 of a run that the browser binary was never installed is a preventable
failure, and `startup` is where it gets prevented.

`shutdown` must be idempotent and must run on the error path.

---

## 3. Universal error taxonomy

Every provider maps its backend's failures onto this closed set. The core
handles these codes and never inspects a vendor error.

```python
class ProviderError(OrchestratorError):
    code: ErrorCode
    domain: Domain
    provider_id: ProviderId
    retryable: bool
    retry_after: float | None
    cause: str | None        # vendor detail as a STRING, never an object
```

| `ErrorCode` | Meaning | Retryable | Typical mapping |
|---|---|---|---|
| `UNAVAILABLE` | Backend unreachable or not started | yes | connection refused, binary missing |
| `TIMEOUT` | Exceeded deadline | yes | read timeout, gate timeout |
| `RATE_LIMITED` | Throttled; `retry_after` set when known | yes | HTTP 429 |
| `OVERLOADED` | Backend transiently saturated | yes | HTTP 529, container pool exhausted |
| `AUTH_FAILED` | Credential missing, invalid, or expired | **no** | HTTP 401/403, bad SSH key |
| `INVALID_REQUEST` | Malformed request; our bug | **no** | HTTP 400, bad command spec |
| `NOT_SUPPORTED` | Capability not offered by this provider | **no** | streaming asked of a non-streaming backend |
| `QUOTA_EXCEEDED` | Hard account or budget limit | **no** | billing limit reached |
| `REFUSED` | Backend declined on policy grounds | **no** | model safety refusal, forge permission denial |
| `CANCELLED` | Cancelled via the context | **no** | operator cancellation |
| `CONFLICT` | State precondition violated | **no** | merge conflict, branch exists |
| `INTERNAL` | Provider bug | **no** | unhandled translation gap |

Three rules that are easy to get wrong and expensive when they are:

1. **`REFUSED` is not an error condition in every domain.** For models it is a
   *result* carried on the response (§5.1), because a refusal is a successful
   HTTP 200 with a stop reason — raising there would crash a caller that has done
   nothing wrong. It is an error only where the operation genuinely could not
   proceed.
2. **`INVALID_REQUEST` is never retried.** Retrying our own malformed request
   burns budget to produce the identical failure.
3. **A provider that cannot classify a failure raises `INTERNAL`**, not
   `UNAVAILABLE`. Guessing "probably transient" turns one bug into an hour of
   retries.

---

## 4. Capability negotiation

A port describes what *may* be asked. A `CapabilitySet` describes what *this
implementation actually does*. Callers query before relying on anything optional.

```python
@dataclass(frozen=True)
class CapabilitySet:
    features: frozenset[Feature]         # boolean capabilities
    limits: Mapping[str, int]            # numeric ceilings
    variants: Mapping[str, frozenset[str]]  # enumerated support, e.g. effort levels

    def has(self, f: Feature) -> bool: ...
    def require(self, *f: Feature) -> None: ...   # raises NOT_SUPPORTED
```

Rules:

- **`require()` is called at startup**, not at point of use. A run that needs
  streaming must fail in the first second, not forty minutes in.
- **A capability declared must work.** The conformance suite exercises every
  declared feature and fails a provider that declares one it cannot deliver.
- **A capability absent must degrade loudly.** A provider asked for something it
  did not declare raises `NOT_SUPPORTED`. It does not silently ignore the
  parameter. Silent ignore is how you get a run that costs full price at
  `effort=low` semantics you never received.
- Capability sets may be determined at `startup` (after probing the backend),
  not only statically. A local model server's context limit is a runtime fact.

---

## 5. The ports

Ten domains. Each gets one port, one conformance suite, and a directory under
`orchestrator/provider/<domain>/`.

```
orchestrator/provider/
  base.py            Provider, ProviderContext, CapabilitySet, ProviderError
  registry.py        (domain, name) -> instance
  conformance/       shared per-domain test suites
  model/             anthropic.py  openai.py  ollama.py  fake.py
  agent/             tool_loop.py  agent_sdk.py  subprocess.py  fake.py
  build/             pytest.py  npm.py  cargo.py  make.py  fake.py
  shell/             local.py  container.py  ssh.py  fake.py
  fs/                local.py  memory.py
  vcs/               git.py  fake.py
  forge/             github.py  gitlab.py  none.py
  browser/           playwright.py  cdp.py  none.py
  secrets/           env.py  keyring.py
  telemetry/         sqlite.py  otel.py  null.py
```

### 5.1 `model` — AI backends

The port that matters most, and the one this document exists for. Anthropic,
OpenAI, Ollama, Hermes, a local server, a fake — all equal citizens.

```python
class ModelPort(Provider, Protocol):
    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...
    async def stream(self, req: CompletionRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, req: CompletionRequest) -> TokenCount: ...
```

**Neutral request** — every field is ours; nothing is a vendor shape.

```python
@dataclass(frozen=True)
class CompletionRequest:
    model: str                      # opaque to the core; provider interprets
    system: tuple[TextBlock, ...]
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    max_output_tokens: int
    reasoning: ReasoningMode        # OFF | AUTO
    effort: Effort                  # MINIMAL | LOW | MEDIUM | HIGH | MAXIMUM
    output_format: JsonSchema | None
    cache_hints: tuple[CacheHint, ...]
    stop: tuple[str, ...]
```

**Neutral response** — note what `stop_reason` carries.

```python
@dataclass(frozen=True)
class CompletionResponse:
    content: tuple[ContentBlock, ...]     # Text | Reasoning | ToolCall
    stop_reason: StopReason               # END | MAX_TOKENS | TOOL_CALL
                                          # | STOP_SEQUENCE | REFUSED | PAUSED
    refusal: RefusalDetail | None
    usage: Usage                          # in/out/cached tokens, cost_usd
    model_served: str
```

**Model-domain rules**

| # | Rule |
|---|---|
| M-1 | **A refusal is a result, not an exception.** `stop_reason == REFUSED` with `content` possibly empty. Callers check `stop_reason` before reading `content`. A provider that raises on refusal, or that returns `content[0]` blindly, fails conformance. |
| M-2 | **`reasoning` and `effort` are neutral intents.** Each provider translates or declares `NOT_SUPPORTED`. Neither is ever passed through as a vendor parameter name. |
| M-3 | **No sampling knobs in the neutral request.** `temperature`, `top_p`, and `top_k` are deliberately absent: they are unsupported on some current backends (returning HTTP 400) and semantically incomparable across the rest. Behavior is steered by prompting. A provider needing them for its own backend reads them from its private config, never from `CompletionRequest`. |
| M-4 | **`max_output_tokens` is a neutral ceiling.** Where a backend counts reasoning tokens against the same budget, the provider documents that in its capability limits. |
| M-5 | **Streaming is mandatory above `limits["stream_above_tokens"]`.** Providers whose SDK times out on large non-streaming requests declare this limit and the core respects it. |
| M-6 | **Never silently truncate.** Input over the context window raises `INVALID_REQUEST` with the measured and permitted sizes (NFR-1.4). Compaction, where supported, is explicit and declared. |
| M-7 | **`Usage.cost_usd` is populated or explicitly `None`.** A provider that cannot price its own calls says so rather than reporting zero. |
| M-8 | **Cache hints are advisory.** Providers without prefix caching ignore them and do not declare `PROMPT_CACHING`. Those with it place breakpoints per spec §9. |

**Model capabilities**

| Feature | Meaning |
|---|---|
| `STREAMING` | `stream()` yields incrementally |
| `TOOL_CALLING` | tools in request, `ToolCall` blocks in response |
| `PARALLEL_TOOL_CALLS` | more than one `ToolCall` per turn |
| `STRUCTURED_OUTPUT` | schema-constrained output |
| `REASONING` | internal reasoning supported |
| `REASONING_VISIBLE` | reasoning content returned, not just signalled |
| `EFFORT_LEVELS` | depth control; `variants["effort"]` lists them |
| `PROMPT_CACHING` | prefix caching; `limits["cache_min_tokens"]` |
| `VISION` | image input |
| `REFUSAL_SEMANTICS` | can return `REFUSED` (absence means it never will) |
| `SERVER_FALLBACK` | can re-serve a refusal on another model in-call |
| `COST_REPORTING` | populates `cost_usd` |
| `TOKEN_COUNTING` | `count_tokens` is exact, not estimated — a **pre-flight** guarantee, answered without running anything |
| `USAGE_REPORTING` | the counts on a response are **measured**, not `estimate_tokens` approximations — a **post-hoc** guarantee |

**`TOKEN_COUNTING` and `USAGE_REPORTING` are separate on purpose** (OP-014).
They were one concept until a backend arrived that has the second and not the
first: the `claude` CLI reports exact usage in its JSON envelope after a call
and offers no way at all to count a prompt beforehand. Conflated, such a
backend must either claim a pre-flight guarantee it cannot keep or label
measurements as estimates. Both are lies; the split is the fix.

`Usage.tokens_estimated` follows `USAGE_REPORTING`, not `TOKEN_COUNTING`.

**Limits:** `max_context_tokens`, `max_output_tokens`, `stream_above_tokens`,
`cache_min_tokens`.

**Translation obligations — the Anthropic reference implementation.** These are
recorded because getting them wrong yields HTTP 400s, and because they
demonstrate what "translate, don't pass through" means in practice. The
constraints live in this one file and nowhere else in the codebase.

| Neutral | Anthropic translation |
|---|---|
| `reasoning=AUTO` | `thinking={"type": "adaptive"}` |
| `reasoning=OFF` | `thinking={"type": "disabled"}` — **valid only at effort `high` or below**; the provider rejects `OFF` + `MAXIMUM` client-side as `INVALID_REQUEST` rather than letting the API 400 |
| `effort` | `output_config={"effort": low\|medium\|high\|xhigh\|max}`; `variants["effort"]` reports all five |
| — | **`budget_tokens` is never sent** (removed; returns 400). Depth is effort, only. |
| — | **`temperature`/`top_p`/`top_k` never sent** (removed; return 400). Consistent with M-3. |
| `max_output_tokens` | `max_tokens`, which bounds reasoning **plus** text; `stream_above_tokens ≈ 16000` |
| `output_format` | `output_config={"format": {"type": "json_schema", ...}}` — assistant prefill is unavailable (400) and is not used |
| `stop_reason=REFUSED` | `stop_reason == "refusal"` on a **200 response**, read before `content` |
| `SERVER_FALLBACK` | server-side fallback opted in by default so a policy decline is re-served rather than failing the attempt |
| `cache_hints` | `cache_control` breakpoints; `cache_min_tokens` per the configured model |

Default model `claude-opus-5`, per spec §4.1 — configurable, and the core never
names it.

**Other backends.** OpenAI-family, Ollama, Hermes, and local servers each own an
equivalent translation table, written when the provider is implemented and
verified against that vendor's current documentation at that time. Two
predictable divergences, recorded now so they are designed for rather than
discovered: backends that expose sampling parameters but no effort control
declare no `EFFORT_LEVELS` and map effort in their private config or not at all;
backends with no policy-refusal concept do not declare `REFUSAL_SEMANTICS` and
never return `REFUSED`. **Do not copy the Anthropic column into another
provider.** Every cell above is vendor-specific and several are actively wrong
elsewhere.

### 5.2 `agent` — harness backends

What spec §4.1 called *Adapter*: how an agent does work, orthogonal to which
model it uses. **Shipped as `orchestrator/adapter/`**, not as
`provider/agent/<domain>/` — a flat two-module package (`base.py` for the
protocol, `claude_code.py` for the one external harness), not the ten-domain
layout sketched in §5's diagram above. The protocol itself matches this
section's intent closely; only its location and its implementation list
below were aspirational and are now corrected against the real module:

```python
class AgentPort(Protocol):
    async def run(
        self, spec: TaskSpec, ctx: ToolContext, ledger: BudgetLedger,
        *, transcript_sink: Callable[[TranscriptEntry], None] | None = None,
    ) -> AttemptResult: ...
```

Two implementations ship: `agent.runtime.AgentRuntime` (default — our loop,
our tools, full audit, workspace-confined per call) and
`adapter.claude_code.ClaudeCodeHarness` (the Claude Code CLI, driven as an
external process — the `EXTERNAL_IO` case A-4 describes below, materialized).
No `agent_sdk` implementation exists or is planned; the Claude Agent SDK and
the Claude Code CLI are different integration surfaces, and the CLI is the one
that shipped.

| # | Rule |
|---|---|
| A-1 | Enforce all three budget axes — wall-clock, tokens, tool calls — and terminate as `budget_exhausted` with partial work preserved (FR-2.6, FR-2.7). |
| A-2 | Emit a transcript event per message, tool call, and tool result, whatever the backend's native format. A harness that cannot be instrumented declares no `TRANSCRIPT` capability, and the core warns at startup. |
| A-3 | Never accept its own work. `AttemptResult` reports what happened; gating is the workflow engine's job (`020` §3.1). |
| A-4 | All filesystem and command access goes through `FilesystemPort` and `ShellPort` — never directly. This is what makes confinement enforceable for harnesses we did not write. |

A-4 is doing real work: a third-party harness that opens files itself cannot be
confined by us. Such a harness declares `EXTERNAL_IO`, and the core refuses to
run it outside a `container`-backed shell provider — the design intent. In
practice, for `ClaudeCodeHarness`, this is an **accepted gap, not an enforced
one**: there is no `container`-backed shell provider to refuse into, so the
confinement is the worktree boundary plus the CLI's own permission model
(`--add-dir` scoped to the worktree, `Bash` withheld). Recorded and accepted
deliberately in `SECURITY.md`, not silently.

### 5.3 `build` — build, test, and lint tools

Everything that produces a gate verdict: pytest, npm, cargo, make, gradle, tox,
a lint runner, a type checker.

```python
class BuildPort(Provider, Protocol):
    async def detect(self, ws: Workspace) -> bool: ...
    async def prepare(self, ws: Workspace) -> BuildResult: ...
    async def run(self, ws: Workspace, target: BuildTarget) -> Verdict: ...
```

```python
@dataclass(frozen=True)
class Verdict:
    outcome: Outcome            # PASSED | FAILED | ERRORED | TIMED_OUT | SKIPPED
    cases: tuple[CaseResult, ...]
    output: str
    duration_s: float
    coverage: Coverage | None
```

| # | Rule |
|---|---|
| B-1 | **`ERRORED` is never reported as `FAILED`.** A missing binary, an import error in the harness, or a crashed runner is `ERRORED`. Conflating them teaches agents to "fix" a broken harness by editing tests (FR-4.4). |
| B-2 | `FAILED` must name the failing cases when the tool can, and declare `STRUCTURED_REPORT` only if it can. Exit-code-only providers declare it false, and the core adjusts feedback quality accordingly. |
| B-3 | Enforce the timeout by killing the **process group**. An orphaned test process holding a worktree open breaks the next attempt (FR-4.5). |
| B-4 | `detect()` enables auto-configuration and must be side-effect free. |
| B-5 | Runs are confined to the attempt workspace and must not mutate the host outside it. |

Capabilities: `STRUCTURED_REPORT`, `COVERAGE`, `PARALLEL`, `INCREMENTAL`,
`FILTERING`, `RETRY_FAILED_ONLY`.

### 5.4 `shell` — command execution

Local subprocess, container, remote SSH, or a sandbox service. **The security
chokepoint for command execution** (NFR-3.2).

```python
class ShellPort(Provider, Protocol):
    async def exec(self, cmd: CommandSpec, ctx: ExecContext) -> ExecResult: ...
    def stream(self, cmd: CommandSpec, ctx: ExecContext) -> AsyncIterator[OutputChunk]: ...
```

```python
@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]        # NEVER a shell string
    cwd: Path
    env: Mapping[str, str]       # explicit; not inherited wholesale
    timeout_s: float
    stdin: bytes | None
```

| # | Rule |
|---|---|
| S-1 | **`argv` only, never a shell string.** No provider may pass a command through a shell interpreter. This eliminates the injection surface structurally rather than by escaping. |
| S-2 | Enforce the configured **executable allowlist**. A blocklist is not acceptable and cannot be made complete (NFR-3.2). |
| S-3 | Reject `argv` elements containing shell metacharacters even though no shell runs, because a downstream tool may itself invoke one. |
| S-4 | `cwd` must be inside `workspace_root`. Reject otherwise. |
| S-5 | `env` is constructed explicitly. Never inherit the parent environment wholesale — it carries credentials (NFR-3.3). |
| S-6 | On timeout or cancellation, kill the **process group** and reap. No orphans. |
| S-7 | Declare isolation honestly: `NETWORK_ISOLATION`, `FILESYSTEM_ISOLATION`, `PROCESS_ISOLATION`. `local` declares none of them and the core surfaces that when running untrusted work. |

Capabilities: `STREAMING`, `PTY`, `NETWORK_ISOLATION`, `FILESYSTEM_ISOLATION`,
`PROCESS_ISOLATION`, `RESOURCE_LIMITS`.

### 5.5 `fs` — filesystem

Local, in-memory (tests), or container-mounted. **The security chokepoint for
path access** (NFR-3.1).

```python
class FilesystemPort(Provider, Protocol):
    async def read(self, path: Path) -> bytes: ...
    async def write(self, path: Path, data: bytes) -> None: ...
    async def list(self, path: Path, glob: str | None = None) -> tuple[Path, ...]: ...
    async def stat(self, path: Path) -> FileInfo: ...
    async def delete(self, path: Path) -> None: ...
```

| # | Rule |
|---|---|
| F-1 | **Canonicalize every path and verify containment in `workspace_root` before any I/O.** Reject `..` traversal, symlink escape, and absolute paths outside the root. Verified by conformance, not by trust. |
| F-2 | Containment is checked on the **resolved** path, after symlink resolution. Checking the raw string is the classic bypass. |
| F-3 | Writes are atomic where the backend allows (temp file plus rename), so a crash mid-write cannot leave a truncated source file. |
| F-4 | Binary-safe. Text encoding is the caller's concern. |

### 5.6 `vcs` — local version control

Git today. The port is not Git-shaped where it does not need to be, but it does
not pretend a non-DAG VCS would drop in unchanged.

```python
class VcsPort(Provider, Protocol):
    async def is_clean(self, repo: Path) -> bool: ...
    async def create_workspace(self, repo: Path, ref: str, name: str) -> Workspace: ...
    async def destroy_workspace(self, ws: Workspace) -> None: ...
    async def commit(self, ws: Workspace, message: str) -> CommitId: ...
    async def diff(self, ws: Workspace, base: str) -> Diff: ...
    async def merge(self, repo: Path, source: str, target: str) -> MergeResult: ...
    async def has_merged(self, repo: Path, source: str, target: str) -> bool: ...
```

| # | Rule |
|---|---|
| V-1 | `create_workspace` yields an **isolated working tree**. Concurrent workspaces must not interfere (FR-3.1). A provider that cannot isolate declares no `ISOLATED_WORKSPACES` and is rejected for concurrent runs. |
| V-2 | Never modify the operator's checked-out tree or current branch (FR-3.2). |
| V-3 | Destructive operations pass one audited chokepoint and refuse branches OrchestratorPro did not create (FR-3.4, NFR-3.6). |
| V-4 | A conflict is a structured `MergeResult` naming conflicted paths, with **no half-merged index left behind** (FR-3.5). |
| V-5 | `has_merged` exists so recovery is idempotent — after a crash between merge and event write, the core asks rather than assumes (`020` §5.1). |
| V-6 | Never force-push, never rewrite published history, never merge to the default branch (FR-3.6). |

### 5.7 `forge` — hosted repository services

GitHub, GitLab, Gitea, Bitbucket — and `none`, the default. Deliberately separate
from `vcs`: local Git and a hosting API are different failure domains, different
credentials, and different availability.

```python
class ForgePort(Provider, Protocol):
    async def open_change_request(self, req: ChangeRequest) -> ChangeRef: ...
    async def get_status(self, ref: ChangeRef) -> ChangeStatus: ...
    async def comment(self, ref: ChangeRef, body: str) -> None: ...
    async def get_checks(self, ref: ChangeRef) -> tuple[CheckResult, ...]: ...
```

| # | Rule |
|---|---|
| G-1 | The **default provider is `none`**, which declares no capabilities and raises `NOT_SUPPORTED`. Forge integration is opt-in; a run must work with no network identity at all. |
| G-2 | Neutral vocabulary: "change request", not "pull request" or "merge request". |
| G-3 | Never merges a change request. That is the human's decision (`000` §4). |
| G-4 | Rate limits map to `RATE_LIMITED` with `retry_after` populated where the service reports it. |

### 5.8 `browser` — web automation

Playwright, a CDP client, a remote browser service, or `none`.

```python
class BrowserPort(Provider, Protocol):
    async def open(self, url: str, ctx: ProviderContext) -> PageRef: ...
    async def snapshot(self, page: PageRef) -> PageSnapshot: ...   # structured, not pixels
    async def act(self, page: PageRef, action: PageAction) -> ActionResult: ...
    async def extract(self, page: PageRef, query: ExtractQuery) -> ExtractResult: ...
    async def screenshot(self, page: PageRef) -> bytes: ...
    async def close(self, page: PageRef) -> None: ...
```

| # | Rule |
|---|---|
| W-1 | **Page content is untrusted input.** It reaches a model as data, never as instruction. Prompt-injection resistance is the caller's obligation, and the port's job is to keep the boundary legible. |
| W-2 | `snapshot` returns a **structured accessibility-style tree**, not raw HTML and not only pixels, so an agent can act without a vision capability. |
| W-3 | Navigation is allowlist-constrained by config. Default deny. |
| W-4 | Never persists cookies or storage across runs unless explicitly configured, and never writes them into a transcript. |
| W-5 | Default provider is `none`. Browser automation is opt-in. |

Capabilities: `JAVASCRIPT`, `SCREENSHOTS`, `NETWORK_INTERCEPT`,
`PERSISTENT_SESSION`, `FILE_DOWNLOAD`, `HEADED`.

### 5.9 `secrets` — credential resolution

Environment, OS keyring, or an external vault.

```python
class SecretsPort(Provider, Protocol):
    async def get(self, key: str) -> SecretRef: ...
    async def resolve(self, ref: SecretRef) -> str: ...   # audited, narrow
```

| # | Rule |
|---|---|
| X-1 | `SecretRef` is an **opaque handle**. It is what circulates; the plaintext is resolved as late as possible, at the point of use. |
| X-2 | `SecretRef.__str__` and `__repr__` return a redacted placeholder, so an accidental log or f-string cannot leak. |
| X-3 | Resolved plaintext must never enter a prompt, a transcript, or a log (NFR-3.3). |
| X-4 | Credentials are never read from a config file (NFR-3.5). |

For the model domain this means the core does not handle API keys at all: the
provider constructs a zero-argument vendor client and lets the SDK resolve its
own credential chain.

### 5.10 `telemetry` — metrics and cost sink

SQLite (default), OpenTelemetry, or null.

```python
class TelemetryPort(Provider, Protocol):
    def emit(self, event: TelemetryEvent) -> None: ...   # non-blocking, never raises
    async def flush(self) -> None: ...
```

| # | Rule |
|---|---|
| T-1 | `emit` **never raises and never blocks.** Telemetry failure must not fail a run. |
| T-2 | Buffering is bounded and drops oldest on overflow, with the drop itself counted. |
| T-3 | Never records secret values or full prompt bodies — identifiers and counts only. |

---

## 6. The registry

One resolution point, config-driven, no import-time side effects.

```python
class Registry(Protocol):
    def get(self, domain: Domain, role: str = "default") -> Provider: ...
    def register(self, factory: ProviderFactory) -> None: ...
    async def startup_all(self) -> tuple[HealthReport, ...]: ...
    async def shutdown_all(self) -> None: ...
```

```toml
[providers]
model      = "anthropic"
agent      = "tool_loop"
build      = "pytest"
shell      = "local"
fs         = "local"
vcs        = "git"
forge      = "none"
browser    = "none"
secrets    = "env"
telemetry  = "sqlite"

# Roles: one domain, several configured instances.
[providers.model.roles]
planner    = { provider = "anthropic", model = "claude-opus-5", effort = "maximum" }
worker     = { provider = "anthropic", model = "claude-opus-5", effort = "high"    }
summarizer = { provider = "ollama",    model = "<local-model>", effort = "minimal" }

[providers.shell.local]
allowlist = ["git", "python", "pytest", "node", "npm"]
```

Rules:

- **Providers are registered by explicit factory**, never by import side effect
  or plugin auto-scan. Discovery-by-import makes startup order load-bearing.
- **`startup_all()` runs before any task starts** and fails the run on any
  unhealthy required provider. Optional providers may degrade, and the
  degradation is logged at warning level.
- **Roles are per-domain.** The `model` domain uses them heavily; most domains
  use only `default`.
- Provider names are **stable strings**, part of the config contract.

---

## 7. What this buys, concretely

| Change | Blast radius |
|---|---|
| Swap Anthropic → Ollama | One config line. `provider/model/ollama.py` must exist and pass conformance. |
| Add Hermes or any new AI backend | One new file under `provider/model/`, one translation table, conformance suite passes. No core change. |
| Run agents in containers | `providers.shell = "container"`. Confinement improves; nothing above changes. |
| pytest → cargo | `providers.build = "cargo"`. Gate semantics identical. |
| Add GitHub PR creation | `providers.forge = "github"` plus a credential. Default stays `none`. |
| Replace Git | Implement `VcsPort`. Honest note: V-1's isolation guarantee is Git-worktree-shaped, and a VCS without an equivalent will need a genuine design conversation, not just a new file. |
| Test any layer offline | Fake providers in every domain, all passing the same conformance suites. |

The last row is the one that pays daily. Every layer above the ports is testable
with no network, no Git, no browser, and no model — deterministically.

---

## 8. Anti-patterns

Explicitly forbidden. Each has been seen to destroy a provider abstraction.

| # | Anti-pattern | Why it is fatal |
|---|---|---|
| AP-1 | `if provider_id == "model.anthropic": ...` in core | The abstraction is now decoration. One conditional becomes twenty. |
| AP-2 | Leaking a vendor type through a port (including inside a `dict`) | Callers bind to it, and the next provider cannot satisfy them. |
| AP-3 | `**kwargs` passthrough to the backend | An untyped, undocumented, vendor-specific side channel that silently breaks on swap. |
| AP-4 | Declaring a capability "close enough" | Silent degradation. The caller believes something false and pays for it later. |
| AP-5 | A "temporary" direct SDK import above the provider layer | It is never temporary. The layering test exists to make this impossible to merge. |
| AP-6 | One `ProviderError` for everything | Retry logic degenerates to retrying auth failures forever. |
| AP-7 | Modelling the union of all vendor features | The port becomes unimplementable for everyone except the vendor it was traced from. |
| AP-8 | Vendor-shaped naming in neutral types (`pull_request`, `system_prompt_blocks`) | Encodes one vendor's model into the contract and quietly forbids the others. |
| AP-9 | A provider reaching global state or config directly | Two instances interfere; tests become order-dependent. |
| AP-10 | Skipping conformance because "it's just a thin wrapper" | Thin wrappers are exactly where error mapping and cancellation get skipped. |

---

## 9. Adding a provider — checklist

1. Pick the domain. If none fits, propose a new port in this document **first** —
   ports are added by amendment, not by improvisation.
2. Create `orchestrator/provider/<domain>/<name>.py`.
3. Implement the substrate (§2) and the port (§5).
4. Write the translation table in the module docstring, verified against the
   backend's current documentation on the day it is written.
5. Declare capabilities honestly. When in doubt, declare less.
6. Map every backend failure onto §3. No unmapped exception escapes.
7. Pass the domain conformance suite unmodified. **Changing the suite to make a
   provider pass requires review** — the suite is the contract.
8. Add provider-specific tests for translation edge cases.
9. Register the factory; document the config block.
10. Confirm the layering test still passes.

---

## 10. Conformance

The mechanism that makes substitutability a fact rather than an aspiration.
Every provider runs the same suite for its domain, unmodified.

### 10.1 Universal conformance — every domain

| Check | Asserts |
|---|---|
| Isolation | Two instances with different config do not interfere |
| Lifecycle | `startup` → `health` → `shutdown`; `shutdown` idempotent and runs on the error path |
| Capability honesty | Every declared feature demonstrably works; every undeclared one raises `NOT_SUPPORTED` |
| Error mapping | Injected backend failures produce the correct `ErrorCode` and `retryable` |
| Cancellation | Cancel mid-operation returns promptly, leaves no orphan process or file |
| No vendor leakage | Reflective scan of returned objects finds no type from outside `orchestrator` |
| Secret hygiene | Configured secret values appear in no log, transcript, or `repr` |
| Config validation | Malformed config raises `ConfigError` at startup, not later |
| **Layering** | Static import scan: no vendor SDK imported outside its provider module |

The layering check is the enforcement arm of §0. It scans the import graph and
fails if any module outside `orchestrator/provider/<domain>/` imports a
registered vendor package.

### 10.2 Per-domain suites

| Domain | Additional checks |
|---|---|
| `model` | Refusal returns `REFUSED` rather than raising; oversized input raises rather than truncating; usage accounting non-zero; streaming and non-streaming agree on final content; `count_tokens` monotonic in input size |
| `agent` | All three budget axes terminate cleanly; partial work preserved; transcript completeness |
| `build` | Red suite → `FAILED` with case names; broken harness → `ERRORED`; hanging suite → `TIMED_OUT` with process group reaped |
| `shell` | Allowlist enforced; metacharacters rejected; `cwd` escape rejected; timeout kills the group; env not inherited wholesale |
| `fs` | `..`, symlink escape, and outside-root absolutes all rejected — on the resolved path |
| `vcs` | Concurrent workspaces independent; host tree untouched; conflict structured with no half-merged index; `has_merged` accurate after a simulated crash |
| `forge` | Rate limit → `RATE_LIMITED` with `retry_after`; never merges |
| `browser` | Navigation allowlist enforced; no session persistence by default; snapshot is structured |
| `secrets` | `repr` and `str` redacted; plaintext never logged |
| `telemetry` | `emit` never raises; bounded buffer drops oldest and counts the drop |

---

## 11. Open questions

Added to spec §13. Resolve before the dependent milestone; do not settle by drift.

| # | Question | Needed by | Working assumption |
|---|---|---|---|
| Q6 | ~~Which non-Anthropic model provider is implemented **first**, and when?~~ **Resolved, and gone further than asked.** `openai_compat` (Ollama/vLLM/LM Studio/Open WebUI) landed as planned; `hermes` and `claude_cli` (M18) followed. Three non-Anthropic implementations now validate the port, not one. | M2 | Ollama or an OpenAI-compatible endpoint, implemented in M2 as the validating second implementation |
| Q7 | Do local/open-weights backends need a distinct port, given differing tool-calling fidelity and no refusal semantics? | M2 | No — capability flags are sufficient |
| Q8 | Is `container` the default shell in v1, or does `local` remain default with containers opt-in? | M6 | `local` default; container recommended and documented for untrusted work |
| Q9 | Does `forge` belong in v1 at all, or is it post-v1? | M11 | Port defined now, `none` ships, first real implementation post-v1 |
| Q10 | Does `browser` belong in v1, given W-1's injection surface? | M11 | Port defined now, `none` ships, implementation post-v1 |
| Q11 | Do ports need semantic versioning once third-party providers exist? | Post-v1 | Not yet — single repository, single owner |

Q6 was the load-bearing one. **A port validated by exactly one real
implementation plus a fake is a hypothesis, not an abstraction** — the second
implementation is what proves the seam is in the right place. That is why it
was pulled forward into M2 rather than deferred, and with four real
implementations now behind the port, the hypothesis has been tested rather
than assumed: the differences among them showed up exactly where predicted
(§5.1 and `docs/020` §6).

---

## 12. Amendments required to existing documents

**Superseded — recorded as history, not a live plan.** This section proposed
landing all ten amendments in the M1 commit. Eighteen milestones later, at
M18, essentially none did: the spec still defines **Adapter** (not retired),
the layer diagram still shows `adapter/`, `git_manager/`, `test_runner/` as
three separate L1 packages (not folded into a ten-domain `provider/`), and
`docs/010`/`TASKS.md` were never re-scoped this way. What actually shipped is
different and narrower — a flat `provider/` with four `model`-domain
implementations, and a flat `adapter/` for the `agent` domain alone — not the
ten-domain unification sketched below. Left unedited rather than rewritten,
so the historical proposal stays legible; do not treat any row below as
pending work.

This document changes decisions recorded elsewhere. Those files are **not**
edited here; the edits below are proposed for approval and should land in the M1
commit so nothing is silently diverged.

| Document | Amendment |
|---|---|
| `ORCHESTRATOR_PRO_SPEC.md` §2 | Redefine **Provider** as "replaceable implementation of an external capability". Retire **Adapter**; replace with the `agent` domain. |
| `ORCHESTRATOR_PRO_SPEC.md` §4 | Layer diagram: `provider/` becomes the full port surface at L0/L1. `adapter/`, `git_manager/`, and `test_runner/` become the `agent`, `vcs`, and `build` domains inside it. |
| `ORCHESTRATOR_PRO_SPEC.md` §4.1 | Replace the LLM-only `Provider` protocol with a reference to this document. Move the Anthropic binding rules to §5.1's translation table as their canonical home. |
| `ORCHESTRATOR_PRO_SPEC.md` §7 | Config gains the `[providers]` block from §6. |
| `ORCHESTRATOR_PRO_SPEC.md` §8 | Error table adopts the §3 taxonomy. |
| `ORCHESTRATOR_PRO_SPEC.md` §13 | Add Q6–Q11. |
| `docs/020_ARCHITECTURE.md` §1 | Redraw layers: ten provider domains at the base rather than three separate L1 packages. |
| `docs/020_ARCHITECTURE.md` §1.1 | Extend chokepoints to all ten domains. |
| `docs/010_REQUIREMENTS.md` | Add FR-8 (provider substitutability) and NFR-6 (conformance); map FR-7.x onto the new port names. |
| `TASKS.md` | Re-scope M2 (all ports + registry + conformance harness, plus the Q6 second model provider), M4 → `vcs` domain, M5 → `build` domain, M6 → `agent` domain. Add `shell`/`fs` to M1. |

**Milestone impact, stated plainly.** M2 grows: it now delivers the substrate,
the registry, the conformance harness, and two real model providers rather than
one. M1 grows slightly, since `shell` and `fs` are needed early and are cheap.
M4, M5, and M6 mostly get renamed rather than enlarged — the work was already
scoped, and it is now scoped behind a port.

This is a real cost, paid up front, in exchange for the property the instruction
asks for: **no dependency on any specific AI, tool, or service anywhere in the
core.** It is worth stating that the cost is not zero and lands almost entirely
on M2.
