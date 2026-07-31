# OrchestratorPro — Technical Specification

**Status:** v1.0 — authoritative
**Last updated:** 2026-07-31
**Supersedes:** the informal specification that stood on `main` before the v1.0
merge (`9d42c4f`). That document sketched the same system in outline; this one is
the version the code was built against and amended alongside.

This document is the source of truth for OrchestratorPro. Where this file and the
code disagree, this file wins until it is amended. Amendments are made by editing
this file in the same commit as the code that motivates them.

---

## 1. Purpose

OrchestratorPro is a **self-hosted control plane for fleets of AI coding agents**.

It takes a high-level goal against a Git repository ("migrate the HTTP client to
httpx", "add OAuth to the admin API"), decomposes it into a dependency graph of
tasks, dispatches each task to an agent running in an isolated Git worktree, gates
completion on the project's own test suite, and merges the results back.

The system is designed so that a human supervises **outcomes**, not keystrokes.

### 1.1 What it is not

- Not a hosted SaaS. It runs on the operator's machine or their own server.
- Not an IDE or an editor plugin.
- Not a model. It orchestrates models through a provider abstraction.
- Not a replacement for CI. It *consumes* the project's tests; it does not define them.
- Not a general workflow engine. The domain is software change on a Git repository.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **Goal** | A human-authored statement of desired change, scoped to one repository. |
| **Task** | The smallest independently-executable unit of work. Has an ID, a prompt, dependencies, and an acceptance condition. |
| **Task graph** | A DAG of tasks derived from a goal. Edges are `depends_on` relations. |
| **Run** | One execution of a task graph. Has a lifecycle, an audit log, and a terminal status. |
| **Agent** | A runtime that executes exactly one task: an LLM loop plus tools, bounded by a budget. |
| **Adapter** | A pluggable backend that an agent drives, behind `AgentPort`: our own tool loop, or the Claude Code CLI harness. |
| **Provider** | A pluggable LLM vendor client. Anthropic is the reference implementation. |
| **Workspace** | A Git worktree, branch, and filesystem sandbox owned by exactly one task attempt. |
| **Gate** | A check that must pass before a task's work is accepted (tests, lint, human approval). |
| **Attempt** | One try at a task. A task may be retried; each retry is a new attempt with a fresh workspace. |

---

## 3. Requirements summary

Full numbered requirements live in `docs/010_REQUIREMENTS.md`. This section states
the load-bearing ones so the spec is readable standalone.

**Functional**

1. Accept a goal + repository path and produce a reviewable task graph.
2. Execute tasks concurrently, honoring dependency order and a global concurrency cap.
3. Isolate every task attempt in its own Git worktree; no two attempts share a working tree.
4. Run the project's test suite as a merge gate; a failing gate blocks acceptance.
5. Persist every run, task, attempt, tool call, and token cost to durable storage.
6. Expose a REST + WebSocket API for control and live observation.
7. Provide a dashboard that renders run progress, per-task diffs, and cost.
8. Support human approval gates on configurable task classes.
9. Resume an interrupted run without redoing completed tasks.
10. Be driven either by a Python API, a CLI, or a declarative YAML file.

**Non-functional**

- **Determinism of orchestration.** Given the same task graph and the same agent
  outputs, the scheduler makes the same decisions. Model output is not
  deterministic; scheduling must be.
- **Crash-safety.** A kill -9 at any point leaves recoverable state on disk.
- **Cost visibility.** Every token is attributed to a task attempt.
- **No silent truncation.** If input exceeds a context window, the system reports
  it and fails or compacts explicitly — never quietly drops content.
- **Local-first.** No outbound network calls except to the configured LLM provider
  and whatever the repository's own tooling requires.

---

## 4. System architecture

Ten packages under `orchestrator/`, layered so that dependencies point downward
only. A module may import from its own layer and below, never above.

```
Layer 5  dashboard/                  web UI
Layer 4  api/                        FastAPI REST + WebSocket control plane
Layer 3  builder/  planner/  workflow/   builds, construction, DAG execution
Layer 2  agent/       task/          agent runtime, task model + scheduler
Layer 1  adapter/     git_manager/   test_runner/    backends, VCS, gates
Layer 0  provider/    core (config, logging, errors, storage)

         ops/                        hardening, backup, retention, benchmarks
         auth/                       identity, roles, sessions, audit
```

`core` is not a directory in the initial tree; it is created in M1 as
`orchestrator/core/`. Everything else already has a package directory.

`ops/` and `auth/` sit outside the stack rather than inside it. Both are
cross-cutting: `ops` is reached from the API and the CLI and imports only
`core`; `auth` is a self-contained identity store the API depends on and nothing
depends on in turn. Neither is imported by any domain layer, which is what keeps
a run executable with authentication switched off entirely.

### 4.1 Package contracts

#### `orchestrator/provider/` — LLM vendor abstraction

Owns every outbound model call. Nothing above this layer imports a vendor SDK.

```python
class Provider(Protocol):
    name: str
    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...
    async def stream(self, req: CompletionRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, req: CompletionRequest) -> int: ...
```

`CompletionRequest` is vendor-neutral: model id, system prompt, messages, tools,
max output tokens, effort, thinking mode, cache hints. The Anthropic provider
translates it to the Messages API.

**Anthropic reference implementation — binding decisions.** These are pinned here
because getting them wrong produces HTTP 400s, not degraded output.

- Default model: **`claude-opus-5`** — 1M context, 128K max output,
  $5/$25 per million input/output tokens. Configurable per role (see §7).
- Thinking: pass `thinking={"type": "adaptive"}`. On `claude-opus-5` thinking is
  **on by default** when the field is omitted. `budget_tokens` is removed and
  returns 400. Never send it.
- Depth control is `output_config={"effort": ...}` with levels
  `low | medium | high | xhigh | max`. Default `high`; agents doing code changes
  run at `xhigh`.
- `thinking={"type": "disabled"}` is accepted **only at effort `high` or below**;
  pairing it with `xhigh`/`max` returns 400. The provider validates this pairing
  client-side and raises `ProviderConfigError` before issuing the request.
- **Never send `temperature`, `top_p`, or `top_k`** — removed on this model
  family; any value returns 400. Behavior is steered by prompting only.
- `max_tokens` bounds thinking **plus** response text. Requests above ~16 000
  must stream, or the SDK's HTTP timeout fires. The provider streams
  unconditionally for agent turns and uses `.get_final_message()`.
- Assistant-turn prefills return 400. Structured output uses
  `output_config={"format": {"type": "json_schema", "schema": ...}}`.
- **Refusals are HTTP 200**, not exceptions: check `stop_reason == "refusal"`
  before reading `content`. The provider surfaces this as a typed
  `ProviderRefusal` result, never as a crash on `content[0]`.
- Opt into server-side fallback by default: beta header
  `server-side-fallback-2026-07-01` with `fallbacks="default"`, so a policy
  decline is re-served rather than failing the task.
- Prompt caching: minimum cacheable prefix on `claude-opus-5` is **512 tokens**.
  The provider places one `cache_control` breakpoint on the last system block and
  one on the last message of the previous turn. See §9.

**Config surface**

```toml
[provider.anthropic]
model          = "claude-opus-5"
effort         = "xhigh"
max_tokens     = 64000
thinking       = "adaptive"     # adaptive | disabled
```

Credentials are never read from config files. Resolution order is the SDK's own:
`ANTHROPIC_API_KEY`, then `ANTHROPIC_AUTH_TOKEN`, then an `ant auth login`
profile. A zero-arg client is constructed and allowed to resolve; the system does
not prompt for a key when a profile is active.

**Shipped model providers.** Four, all behind the same neutral port:
`anthropic` (the reference implementation above; `claude` is a working alias),
`hermes`, `openai_compat` (Ollama / vLLM / LM Studio / Open WebUI), and
`claude_cli`.

**`claude_cli` — the subscription-billed backend.** Drives the Claude Code CLI
(`claude -p … --output-format text`) so a deployment authenticated by a Claude
subscription needs **no API key anywhere**. Recorded design decision: the CLI
is an agent *harness*, but this provider maps it as a **one-shot `ModelPort`**
— prompt in, text out, no tool calling declared — because that slots into the
existing runtime with zero new protocols. Mapping the CLI as a *harness* (the
`agent` domain of `docs/030` §5.2, the `EXTERNAL_IO` confinement case) is a
**separate, second mapping** — `orchestrator/adapter/claude_code.py`,
described below — not an alternative to this one; both exist, for different
callers. It declares **no** streaming, token counting, cost reporting, prompt caching,
or refusal semantics; token usage is estimated and `cost_usd` is `None`, and
the estimates travel **labelled**: `Usage.tokens_estimated` rides the numbers
through the agent, the `attempt.finished` payload, the projection, and the API
(`usage.tokens_estimated` on a run, `tokens_estimated` per attempt), sticky
under aggregation. The token budget axis still binds on estimates — an
unbounded axis under an unmetered backend is worse than an approximate one —
but a budget-exhausted verdict on estimates says so. The
prompt travels as one `-p` argument (the Windows `claude.cmd` shim swallows
piped stdin); a hung invocation has its whole process group killed on timeout;
an expired login maps to `AUTH_FAILED`, non-retryable. Configured via
`[provider.claude_cli]` (`executable`, `timeout_s`); `--model` is never passed
from configuration, because the block's default is the API model id and
forwarding it would silently override the operator's own CLI settings. Cap
`run.max_concurrency` low (2–3) when this provider is bound — subscription
rate limits are undocumented and surface mid-run.

#### `orchestrator/adapter/` — agent backend abstraction

An adapter is *how* an agent does work, distinct from *which model* it uses.
This package was an empty placeholder through v1.0, waiting for a second
harness to justify writing the protocol from more than one implementation and
an imagination — `docs/030` §5.2 said as much. The Claude Code harness (M18)
is that second harness, so the protocol is written now, from two real
implementations:

```python
class AgentPort(Protocol):
    async def run(
        self, spec: TaskSpec, ctx: ToolContext, ledger: BudgetLedger,
        *, transcript_sink: Callable[[TranscriptEntry], None] | None = None,
    ) -> AttemptResult: ...
```

Two implementations satisfy it:

| Implementation | Backend | When to use |
|---|---|---|
| `agent.runtime.AgentRuntime` | Provider + a tool set we define, looping on `stop_reason == "tool_use"` | Default. Full control over the tool surface and the audit log; every call is confined to the workspace by us. |
| `adapter.claude_code.ClaudeCodeHarness` | The Claude Code CLI, driven as an external process that edits the worktree itself | Subscription-billed, no API key present anywhere. Less resolution — no per-call path confinement; the worktree boundary is the confinement instead. |

One rule is load-bearing for either implementation: **an adapter reports what
it did and never decides whether that was acceptable** (`docs/020` §3.1).
Gating belongs to the workflow engine; an adapter that could mark itself green
is an adapter with no gate.

Tool surface exposed by `AgentRuntime`, all scoped to the attempt's workspace:
`read_file`, `write_file`, `list_dir`, `finish`. Every path argument is
canonicalized and rejected if it escapes the workspace root. `run_command` and
`run_tests` are deliberately **not** built in — they need the `shell` and
`build` provider domains (`docs/030` §5.3/§5.4); adding a second, unguarded
way to execute things would undo the point of the allowlist that already
guards `check_command`.

#### `orchestrator/agent/` — agent runtime

One `Agent` executes one attempt. It owns:

- Prompt assembly (system prompt, task spec, repository context, prior-attempt feedback).
- The adapter invocation and its budget.
- Transcript capture: every message, tool call, tool result, and token count.
- Termination: success, gate failure, budget exhaustion, refusal, or hard error.

Budgets are enforced on three axes — wall-clock seconds, total tokens, and tool
calls — whichever binds first. Exhaustion is a normal outcome, not an exception:
the attempt ends with `status=budget_exhausted` and whatever partial work exists
is preserved in the workspace for inspection.

#### `orchestrator/task/` — task model and scheduler

Domain model and the DAG scheduler.

```python
@dataclass(frozen=True)
class Task:
    id: TaskId
    title: str
    prompt: str
    depends_on: tuple[TaskId, ...]
    gates: tuple[GateSpec, ...]
    max_attempts: int
    budget: Budget
    labels: frozenset[str]
```

Task states form a strict machine:

```
pending ──▶ ready ──▶ running ──▶ gating ──▶ succeeded
              ▲          │           │
              │          ▼           ▼
              └──── retrying ◀── failed ──▶ abandoned
                                   │
                              blocked (dependency failed)
```

The scheduler is a pure function of graph + current state → set of tasks to
start. It has no I/O, which makes it exhaustively testable. Cycle detection runs
at graph construction; a cyclic graph is rejected before any work begins.

#### `orchestrator/workflow/` — execution engine

Drives the scheduler, owns the concurrency semaphore, dispatches attempts, applies
gates, and writes state transitions to storage. Handles:

- Global and per-label concurrency caps.
- Retry with feedback: a failed attempt's gate output is fed into the next
  attempt's prompt, so retry #2 knows why #1 failed.
- Cancellation and graceful shutdown (finish in-flight attempts, start nothing new).
- Resume: on startup, reconcile persisted state against on-disk worktrees.

#### `orchestrator/builder/` — project analysis and incremental builds

Answers *what must be rebuilt after this change, and in what order*. Analyzes a
project root into build units identified by content digest, derives the
dependency graph between them, plans the minimal correct rebuild, executes it
through the M3 scheduler, and records what it produced.

The correctness rule the package exists to enforce: a unit whose sources changed
is rebuilt, **and so is every unit downstream of it, transitively**. Skipping
that propagation produces a build that succeeds and a binary that is quietly
inconsistent — the failure that teaches people to delete their output directory
before every build.

Guarantees:

- Change is detected by **content, never timestamps**. A checkout, a clock skew,
  or a file copied back into place does not trigger a rebuild.
- A cache hit is **verified against the disk** before it is honoured. An entry
  whose artifacts are missing or altered is discarded, not trusted.
- A failed build and a broken build tool are **different outcomes**, exactly as
  in §4.1's gate contract (FR-4.4).
- Build output is parsed into **located diagnostics** — file, line, severity —
  not quoted at length (FR-4.3).
- Unit definitions may be written by an agent, so every path in a manifest is
  canonicalized and confined to the project root before use.

It reaches the workflow engine by implementing the same `GateRunner` protocol
the test runner does, so a step can gate on the project still building without
either package importing the other.

**Declarative workflow construction — YAML schema, validation, and the LLM
planner (FR-1.2) — is not in this package.** It is a different subsystem that
happened to share the name; it lives in `orchestrator/planner/`, below.

#### `orchestrator/planner/` — declarative workflow construction

Turns a description of work into a validated, executable `WorkflowDefinition`.
Two front doors, one back door: a hand-written YAML file and an LLM-generated
plan are validated by the same code and compiled by the same code, so they are
equally executable and equally refused when wrong (FR-1.2).

```yaml
name: migrate-http-client
goal: replace requests with httpx across the client
max_concurrency: 3
defaults: {max_attempts: 2, gates: [tests]}
steps:
  - name: client-uses-httpx
    prompt: |
      Replace the requests-based client in src/client.py with httpx.
  - name: call-sites-updated
    prompt: Update every call site to the new signature.
    depends_on: [client-uses-httpx]
    when: {did_work: client-uses-httpx}
```

Guarantees:

- **Every problem is reported at once**, each with a path
  (`steps[2].depends_on[0]`). A validator that stops at the first error turns one
  broken file into five edit-and-retry cycles.
- **A cycle is a load-time error.** Loading compiles the graph, so a file that
  would deadlock is refused by `workflow check` rather than at minute forty.
- **A condition may only reference a step this one depends on.** Otherwise it
  could be evaluated before that step had finished and the answer would depend on
  timing.
- **A workflow file may have been written by an agent.** It is parsed with
  `yaml.safe_load`, never `load`, and its paths are validated like any other
  model-supplied path.
- The LLM planner is constrained by a JSON Schema at generation time and
  validated afterwards anyway. A refusal is surfaced, never retried; only a
  schema violation earns a correction turn.

This is the one package above layer 0 with a runtime dependency that is not a
web-layer concern: **PyYAML**. A hand-rolled YAML subset would accept documents
real YAML rejects, which is a worse failure than the dependency.

#### `orchestrator/git_manager/` — VCS isolation

Every attempt gets `git worktree add` on a fresh branch named
`orchestrator/<run-id>/<task-id>/<attempt>`. On success the branch is merged into
the run's integration branch; on failure the worktree is retained for inspection
and garbage-collected per retention policy.

Guarantees:

- Never operates on the user's checked-out working tree.
- Never force-pushes, never rewrites published history, never deletes a branch
  that has unmerged commits, unless explicitly asked.
- Detects merge conflicts and surfaces them as a task-level failure with the
  conflicted paths, rather than leaving a half-merged index.
- All destructive operations funnel through one audited chokepoint.

#### `orchestrator/test_runner/` — gates

Executes the project's test command in the attempt's workspace, parses results,
and returns a structured verdict. Ships with pytest and JUnit-XML parsers plus a
generic exit-code parser. A gate returns `passed`, `failed` (with the failing
cases and captured output), or `errored` (the runner itself broke — distinct from
tests failing, and never treated as a test failure).

#### `orchestrator/api/` — control plane

FastAPI. REST for CRUD and commands, SSE **and** WebSocket for the live event
stream — the same content either way, because a browser and a script want
different transports and neither should have to translate.

```
GET    /health                          liveness, and what the server can do
GET    /config                          the effective configuration

POST   /runs                            declare a run
GET    /runs                            list runs
GET    /runs/{id}                       run detail with task states
GET    /runs/{id}/status                compact progress, for a poller
POST   /runs/{id}/cancel                stop starting new work
POST   /runs/{id}/resume                continue from the persisted log

POST   /runs/{id}/tasks                 add a task
GET    /runs/{id}/tasks                 list, filterable by state
GET    /runs/{id}/tasks/{task_id}
PATCH  /runs/{id}/tasks/{task_id}       amend one that has not started
DELETE /runs/{id}/tasks/{task_id}       retire one that has not started

POST   /workflows                       register a definition
GET    /workflows, /workflows/{name}
DELETE /workflows/{name}
POST   /workflows/{name}/runs           execute it

GET    /agents/roles                    resolved per-role settings
GET    /agents/tools                    the tool surface
POST   /agents/prompt                   render a prompt without calling a model

POST   /builds/analyze                  scan a project into units
POST   /builds/plan                     what would be rebuilt, and why
POST   /builds                          plan and execute
GET    /builds, /builds/{id}
DELETE /builds/cache

GET    /approvals                       the queue, oldest first
POST   /runs/{id}/tasks/{task_id}/approval          hold for review
POST   /runs/{id}/tasks/{task_id}/approval/resolve  approve | reject | retry
GET    /runs/{id}/tasks/{task_id}/attempts          attempt history
GET    /runs/{id}/tasks/{task_id}/transcript        what the agent did
GET    /runs/{id}/tasks/{task_id}/diff              what it produced

POST   /auth/login, /auth/refresh, /auth/logout, /auth/logout-all
GET    /auth/me, /auth/sessions, /auth/audit
GET|POST /auth/users, /auth/keys        user and API-key administration

GET    /runs/{id}/log                   read the recorded log, paged
GET    /events                          SSE, every run
GET    /runs/{id}/events                SSE, one run, replay then live
WS     /runs/{id}/ws                    the same over a WebSocket
```

Reads replay the event log rather than reading the materialized tables: slower,
and it cannot disagree with the record. Writes are events, so anything the API
does survives a crash and appears in a replay.

Every failure returns `{"error": {code, message, retryable, detail}}` with a
stable `code`, so a client branches on a value rather than on prose.

Binds to `127.0.0.1` by default. Any other bind address requires an explicitly
configured auth token; the server refuses to start on `0.0.0.0` without one.

Paths arriving in a request body — `repo_path`, build paths, manifest sources —
are canonicalized and confined to the configured workspace root before any I/O.
A path from HTTP is exactly as untrusted as one from a model.

**Authorization is a floor plus per-route escalation.** The whole router carries
a viewer dependency, so a new endpoint added without thought is read-only rather
than open; routes that change something declare operator, and administration
declares admin. Roles are ordered and compared with `>=`, not by set membership,
so inserting a role between two existing ones does not silently grant everything.

The dependencies take `HTTPConnection`, not `Request` — a `Request`-typed
dependency cannot be resolved on a WebSocket route, which is exactly how a
streaming endpoint ends up unauthenticated. A `?token=` query parameter is
accepted on `/events` and `/ws` only, because a browser cannot set a header on
`EventSource` or `WebSocket`; anywhere else it would land in access logs for
nothing.

**When no account exists the API serves every route as an operator**, and says so
on every start. That is the single-operator default; `auth bootstrap` ends it.

#### `orchestrator/auth/` — identity

Users, API keys, sessions, roles, and an append-only audit log. Three roles,
ordered: `viewer < operator < admin`.

- **JWT is HS256 on the standard library** — `hmac`, `hashlib`, `base64`. One
  algorithm; the header's `alg` is checked but *never used to select a verifier*,
  which is the `alg: none` and RS256-confusion class of attack. Every claim is
  verified — `exp`, `nbf`, `iss`, `aud`, and the token kind — with 30 seconds of
  leeway. Access tokens last 15 minutes, refresh tokens 30 days.
- **Passwords are scrypt** (n=2^14, r=8, p=1) with a self-describing
  `scrypt$n$r$p$salt$hash` encoding, so the parameters can be raised without
  invalidating existing hashes. API keys are `opk_`-prefixed, shown once, and
  stored as SHA-256.
- **Authentication is uniform.** An unknown user is hashed anyway so timing does
  not distinguish it; a disabled account, a wrong password, and a missing account
  are the same 401; all of them are audited.
- **The audit log is protected by the engine**, with the same append-only
  triggers as the event log. Auditing never raises — a failure to record must not
  take down the operation being recorded.
- Changing a password, a role, or an active flag revokes that user's sessions.
  `delete_user` refuses the last admin and `set_role` refuses self-demotion;
  both are how an installation locks itself out.

#### `orchestrator/ops/` — operations

Hardening, rate limiting, backup, configuration migration, retention, and
benchmarks. Reached from the API and the CLI; imports only `core`.

- **Hardening fails closed.** `verify_deployment` raises rather than warns, and
  a non-loopback bind is refused unless the deployment names its allowed hosts,
  enables rate limiting, and has a token variable configured.
- **Middleware order is load-bearing.** Security headers are outermost so a 429
  carries them too; the request log wraps the rate limiter so a refused caller is
  the one you can find afterwards. Streaming endpoints are exempt from the
  bucket — a long-lived SSE connection is one request, not a flood.
- **Retention archives before it prunes, and verifies before it deletes.** An
  archive is gzipped JSON with a digest, and verification replays it to the same
  state rather than merely checksumming the bytes. Pruning drops the append-only
  triggers and restores them inside the same transaction — there is no other way
  to delete from a table the engine protects, and one transaction is what stops a
  crash mid-prune from leaving the log editable.
- `apply_retention` previews by default. A policy that deletes without archiving
  says so in capital letters.

#### `orchestrator/dashboard/` — UI

Single-page app served by the API, mounted at `/ui`. Plain ES modules and CSS,
no build step, no bundler, no package manager, and nothing loaded from another
origin — so the dashboard cannot rot independently of the backend, and it works
on a machine with no internet access.

Pages: runs and one run in full (task list, task graph, log), workflows and one
definition's graph, agents (roles, tools, prompt preview), builds (history, plan
preview, per-unit diagnostics), the live event viewer, metrics, configuration,
and system status.

**It consumes the API and nothing else.** The backend serves one HTML document
and a directory of static files; it imports nothing from `core`, `task`,
`workflow`, or `builder`, and a test enforces that. A dashboard that reaches
past the API grows a second idea of what a run is, and the first time the two
disagree nobody can say which is right.

Two invariants inside the frontend, both tested:

- **One module talks to the network.** Facts enter the program through
  `api.js`; every other module receives data and returns nodes.
- **No markup is built from a string.** Goals, prompts, and diagnostics are
  agent output; the UI writes them as text, never as HTML.

**The approval queue, transcripts, and diffs exist in the API but not yet in the
UI.** A reviewer can read attempt history, a transcript, and a diff, and record a
decision, over HTTP; there is no page for it. That is a gap in the dashboard, not
in the system, and it is recorded rather than implied.

---

## 5. Data model and persistence

SQLite, one file per installation at `~/.orchestratorpro/state.db` (overridable).
SQLite is chosen for crash-safety and zero-ops, not performance; the workload is
dozens of writes per second at most.

**Via stdlib `sqlite3`, not SQLAlchemy** — resolved in M10. The schema is a
handful of flat tables and the durability guarantees come from WAL plus
`synchronous=FULL`, neither of which an ORM would supply. Keeping it means every
layer below the API runs on the standard library, which a test enforces.

```
runs         (id, goal, repo_path, status, created_at, finished_at, config_json)
tasks        (id, run_id, title, prompt, depends_on_json, state, ...)
attempts     (id, task_id, n, adapter, workspace_path, branch, status,
              started_at, finished_at, tokens_in, tokens_out, cost_usd)
events       (id, run_id, task_id, attempt_id, ts, type, payload_json)
gate_results (id, attempt_id, gate, verdict, detail_json)
approvals    (id, task_id, requested_at, resolved_at, decision, actor)

users        (username, password_hash, role, active, created_at)
api_keys     (id, username, key_hash, label, expires_at, revoked_at)
sessions     (id, username, issued_at, expires_at, revoked_at)
audit_log    (id, ts, actor, action, target, detail_json)
```

The four authentication tables live in the same file but are created and owned by
`auth/store.py`, not by the core migration. `audit_log` carries the same
append-only triggers as `events`.

An approval is an **event** as well as a row: requesting and resolving both
append, so the decision, its author, and its timing survive a rebuild, and
resolving emits its own consequence — `TASK_ABANDONED` on reject, `TASK_READY`
on retry — rather than requiring a special case in the projection.

`events` is append-only and is the audit log. Run state is *derivable* from
events; the other tables are materialized views maintained transactionally
alongside the event write. This is what makes resume correct: replay events, get
state.

Transcripts (full message histories, often megabytes) are **not** stored in
SQLite. They are written as JSONL under
`~/.orchestratorpro/transcripts/<run>/<task>/<attempt>.jsonl`, referenced by path.

---

## 6. Execution lifecycle

```
1. plan     goal + repo  ──▶ task graph (LLM planner or YAML)
2. approve  human reviews the graph            [skippable via auto_approve]
3. prepare  create integration branch, validate repo is clean
4. execute  loop:
              scheduler yields ready tasks
              for each, up to the concurrency cap:
                git_manager.create_workspace()
                agent.run() via adapter
                test_runner gates
                on pass  → merge to integration branch, mark succeeded
                on fail  → retry with feedback, or mark failed
              until no task is ready and none running
5. finish   report: diffs, cost, failures; leave integration branch for review
```

The system never merges the integration branch into `main`. That is the
operator's call, made with a normal review, outside the tool. This is a
deliberate boundary: OrchestratorPro proposes, the human disposes.

---

## 7. Configuration

`orchestrator.toml` in the repository root, or `~/.orchestratorpro/config.toml`
as a fallback. Repository-local values win.

```toml
[run]
max_concurrency = 4
auto_approve    = false

[provider.anthropic]
model      = "claude-opus-5"
effort     = "xhigh"
max_tokens = 64000

# Alternative, subscription-billed backend (M18): no API key anywhere.
# Mutually exclusive with [provider.anthropic] in practice — bind whichever
# one the registry should resolve for the "model" domain.
[provider.claude_cli]
executable = "claude"
timeout_s  = 900.0

# Per-role model overrides. Planning is one-shot and reasoning-heavy;
# summarization is high-volume and cheap.
[provider.roles]
planner    = { model = "claude-opus-5",  effort = "max"  }
worker     = { model = "claude-opus-5",  effort = "xhigh" }
summarizer = { model = "claude-haiku-4-5", effort = "low" }

[agent]
budget_seconds    = 1800
budget_tokens     = 2000000
budget_tool_calls = 200
adapter           = "tool_loop"
model             = "sonnet"      # the agent's model; "" = let the CLI decide

[git]
integration_branch = "orchestrator/integration"
retain_failed_worktrees = true

[gates]
test_command = "pytest -q"
parser       = "pytest"
timeout_s    = 900.0
# env         = { KEY = "value" }   # optional, additive to the inherited env

[api]
host = "127.0.0.1"
port = 8765

[security]
allowed_hosts           = []          # required for any non-loopback bind
max_body_bytes          = 1048576
rate_limit_per_minute   = 600
rate_limit_burst        = 120
cors_origins            = []          # "*" is refused outright
security_headers        = true
request_log             = true
shutdown_grace_s        = 20.0
```

Every key can also be set from the environment, which is the layer a deployment
controls without rebuilding anything: `ORCHESTRATORPRO__API__PORT=8080` sets
`[api] port`. Two underscores separate the levels, and the environment overrides
both files. Empty means unset, so a blank line in a `.env` cannot override a
real configuration file.

`config_version` records the shape a file was written for. Configuration
validation is strict — an unknown key is an error — so a renamed key would break
every installation on upgrade. `orchestratorpro config migrate` is the other
half of that bargain: it previews by default, keeps the original, and reports
what it moved rather than dropping it.

Note `budget_tokens` here is **OrchestratorPro's own spend cap**, unrelated to the
removed Anthropic `thinking.budget_tokens` parameter. The name collision is
unfortunate but the config key is ours and the distinction is enforced in code —
this value never reaches a request body.

---

## 8. Error handling

Every error carries a stable `code`, a human message, and a `retryable` flag.

| Class | Examples | Disposition |
|---|---|---|
| `ConfigError` | bad TOML, invalid model id, thinking/effort conflict | Fail fast at startup. Never retried. |
| `ProviderError` | 429, 5xx, connection reset | Retried with exponential backoff by the SDK, then by us. |
| `ProviderRefusal` | `stop_reason == "refusal"` | Not retried with the same prompt. Falls back per §4.1, else fails the attempt with the refusal category. |
| `BudgetExhausted` | wall-clock/token/tool-call cap hit | Normal terminal state for an attempt. Triggers retry only if attempts remain and the budget was raised. |
| `GateFailure` | tests red | Triggers retry-with-feedback. |
| `WorkspaceError` | worktree creation failed, merge conflict | Attempt fails; worktree retained. |
| `InternalError` | our bug | Logged with full context, run halted, state left recoverable. |

A gate *erroring* (the test runner itself broke) is never reported as tests
failing. Conflating the two would teach agents to "fix" a broken harness.

---

## 9. Prompt and context strategy

Caching is a prefix match: any byte change invalidates everything after it.
Prompt assembly is therefore ordered by stability, most stable first.

```
[ tools            ]  frozen per adapter, sorted by name
[ system prompt    ]  frozen per role — no timestamps, no run ids   ◀── cache breakpoint
[ repo context     ]  file tree + conventions, stable per run
[ task spec        ]  stable per task
[ attempt feedback ]  varies per attempt
[ conversation     ]  varies per turn                               ◀── cache breakpoint
```

Rules enforced by review and by a unit test that hashes a rendered prompt twice:

- No `datetime.now()`, UUIDs, or run identifiers in the system prompt.
- Tool lists are sorted deterministically; JSON is serialized with sorted keys.
- The tool set does not change mid-conversation. Modes are conveyed as message
  content, not by swapping tools.
- Long-running attempts enable server-side compaction rather than truncating
  history locally.

---

## 10. Security

- **Path confinement.** Every filesystem tool canonicalizes its path and verifies
  containment within the workspace root. Traversal, symlink escape, and absolute
  paths outside the root are rejected before any I/O.
- **Command allowlist.** `run_command` permits a configured executable list.
  Shell metacharacters (`&&`, `|`, `;`, backticks, `$()`) are rejected — a
  blocklist would not be sufficient.
- **No secrets in prompts.** Credentials are never interpolated into system
  prompts or messages; they would persist in the transcript.
- **API binding.** Localhost by default. A non-loopback bind is *refused* — not
  warned about — unless the deployment names its allowed hosts, enables rate
  limiting, and configures a token variable.
- **Authentication.** Off until an account exists, and loudly so. Once
  bootstrapped: HS256 tokens whose `alg` header is checked but never used to
  select a verifier, scrypt passwords, hashed API keys, ordered roles compared
  with `>=`, sessions revoked on any credential change, and an append-only audit
  log the engine protects.
- **Uniform authentication failures.** Unknown user, wrong password, and disabled
  account are indistinguishable in message and in timing.
- **Destructive Git operations** funnel through one audited function and are
  refused unless the target branch is one OrchestratorPro created.
- The agent is treated as **untrusted input to the harness**, at all times.
- **Continuously checked, not just designed in.** `flake8-bandit`
  (`ruff check orchestrator/ --select S`) is part of the standing lint
  selection; `pip-audit` runs against the pinned `requirements.txt`;
  `tests/ops/test_security.py` fails if `S` leaves the selection, if a waiver
  carries no reason, or if `assert` reappears in `orchestrator/` (asserts
  vanish under `python -O`, so no invariant may rest on one). The threat
  model and every accepted finding are recorded in `SECURITY.md`, not left to
  reviewer memory.
- **`ClaudeCodeHarness` is an accepted, named exception.** It drives an
  external CLI that edits the worktree with its own tools — an `EXTERNAL_IO`
  case (§4.1, `orchestrator/adapter/`) where our path-confinement guards do
  not reach. The worktree boundary and the CLI's own permission model
  (`--add-dir` scoped, `Bash` withheld) are the confinement instead. Recorded
  deliberately in `SECURITY.md`, not silently.

---

## 11. Testing strategy

| Layer | Approach |
|---|---|
| Scheduler | Pure unit tests. Property tests over random DAGs: no task starts before its dependencies, no cycles accepted, concurrency cap never exceeded. |
| Provider | Recorded fixtures. No live API calls in the default suite. A separate `-m live` suite exercises the real API and is never required for a milestone. |
| Adapters | Fake provider returning scripted tool-call sequences. |
| git_manager | Real Git against temporary repositories. This is the one place where mocking would test nothing. |
| test_runner | Fixture projects with known-passing and known-failing suites. |
| API | `httpx.ASGITransport` against the app, no network. |
| Auth | Written as attacks, not round-trips: `alg: none`, RS256 confusion, tampered payloads, cross-installation tokens, kind confusion, expiry. |
| Planner | A hand-written YAML file and a generated plan compile to graphs asserted equal node for node, then both executed. |
| Retention | Archive, verify, prune, restore against a real database — including a delete attempted afterwards, expecting refusal. |
| End-to-end | A fixture repository, a fake provider, one full run from goal to merged integration branch; plus restart-and-recovery, multi-agent, approvals, load, and a real Docker build. |

`pytest -q` must be green before a milestone is marked complete. No milestone is
complete with skipped tests that hide a failure.

---

## 12. Milestones

Detailed status and checklists live in `TASKS.md`. Summary:

| ID | Milestone | Delivers |
|---|---|---|
| M0 | Specification | This document + `docs/` + `TASKS.md`. |
| M1 | Core foundations | `core/`: config, logging, errors, ids, storage schema. |
| M2 | Provider layer | Anthropic provider, streaming, token counting, refusal handling. |
| M3 | Task model + scheduler | Domain objects, DAG, state machine, scheduler. |
| M4 | Git manager | Worktrees, branches, merges, conflict detection. |
| M5 | Test runner | Execution, pytest/JUnit parsers, verdicts. |
| M6 | Agent runtime + tool-loop adapter | Working single-task execution end to end. |
| M7 | Workflow engine | Concurrency, retries with feedback, resume. |
| M8 | Builder + planner | Project analysis, incremental build planning, build cache. |
| M9 | Workflow construction | YAML schema, validation, LLM planner (FR-1.2). |
| M10 | API | REST + WebSocket, OpenAPI. |
| M11 | Dashboard | Run view, task graph, live event stream, cost. |
| M12 | Hardening | Packaging, CLI entry point, rate limiting, backup, perf pass. |
| M13 | Authentication | JWT, API keys, three roles, sessions, audit log. |
| M14 | Approvals | Queue, decisions, attempt history, transcripts, diffs. |
| M15 | Retention + end-to-end | Archive/verify/prune/restore, and the e2e suite. |
| M16 | Documentation | Installation, API, user, and developer guides. |
| M17 | The served composition root | `serve` actually executes: worktree per attempt, gates, commits, serialized merges to the integration branch. Opened after v1.0, past the original plan. |
| M18 | Provider expansion | `provider/claude_cli.py` — Claude Code as a subscription-billed `ModelPort`, no API key; measured (not estimated) token accounting from its own envelope. |

Ordering is a dependency order, not a wish list. M6 is the first point at which
the system does something useful end to end, and it is deliberately early. M17
and M18 are the first work past the original sixteen-milestone plan — see
`TASKS.md` for why each was opened and `ROADMAP.md` for what comes after.

Two departures from the plan, recorded rather than smoothed over. **M9 was built
after M12** — the milestone that owns FR-1.2 was skipped at the time and picked
up later, so "hardened" briefly meant "hardened and unable to read a workflow
file". And **auth moved out of M10**: M10 shipped deliberately without it,
refusing any non-loopback bind instead, and M13 is where it actually arrived.
M13–M16 are this document's numbers for four workstreams that were requested
without milestone labels; `TASKS.md` carries the mapping.

---

## 13. Open questions

Recorded rather than guessed. Each must be resolved before the milestone that
depends on it.

| # | Question | Needed by |
|---|---|---|
| Q1 | Are non-Anthropic providers actually required, or is the abstraction purely for testability? | M2 |
| Q2 | Should the planner decompose recursively (tasks spawning subtasks) or one level only? | M8 |
| Q3 | Multi-repository runs — in scope, or explicitly one repo per run forever? | M7 |
| Q4 | Does the dashboard need multi-user auth, or is single-operator assumed? | M9 |
| Q5 | Retention policy for failed worktrees and transcripts — time-based, count-based, or manual? | M12 |

**Resolved.**

- **Q1: yes, but thin.** Four provider implementations across two backend
  shapes (`anthropic`, `hermes`, `openai_compat`, and `claude_cli` added in
  M18), and the differences appeared where `docs/030` predicted (M2).
- **Q2: one level.** The planner decomposes a goal into steps and does not
  recurse. A step that turns out to be too large is a prompt problem, and
  recursive decomposition would hide it behind more decomposition (M9).
- **Q3: one repository per run**, honoured throughout (M7).
- **Q4: multi-user, with a single-operator default.** Three ordered roles and a
  full identity store, but with no account bootstrapped the API serves as an
  operator — so the convenience default survives and is not the only option
  (M13).

**Partly open.** Q5: runs, backups, and sessions have retention policies —
archive-verify-prune for runs, count-based for backups, expiry-based for
sessions. Worktrees and transcripts still have none; they accumulate until an
operator removes them. That is the largest thing this build does not do, and it
is stated in the user manual as well as here.
