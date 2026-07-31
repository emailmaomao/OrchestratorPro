# ARCHITECTURE — OrchestratorPro

**Canonical, verified against source** on 2026-07-31 at `main` @ `e86488d`.

Companion documents: `ORCHESTRATOR_PRO_SPEC.md` states *what* each component must
do and remains authoritative on contracts; `docs/020_ARCHITECTURE.md` explains
*why* the structure is shaped this way; `docs/030_PROVIDER_INTERFACE.md` is the
provider law. This file records **what is actually assembled**, including what is
not.

---

## 1. Layers

Dependencies point downward only. Two import-layering tests enforce the parts
that matter: nothing below L4 imports FastAPI, Starlette, Pydantic, or httpx; only
`provider/` names a vendor.

```
L5  dashboard/                                 presentation, plain ES modules at /ui
L4  api/                                       REST + SSE + WS, 44 paths
L3  builder/    planner/    workflow/          builds, construction, execution
L2  agent/      task/                          agent runtime, DAG + scheduler
L1  adapter/         git_manager/  test_runner/
L0  provider/   core/                          model ports, config, storage, events

    ops/    auth/                              cross-cutting; import only core
```

Two same-layer pairs deliberately do not know about each other:

- **`agent` and `task`.** An agent receives a `TaskSpec` — a narrow value object
  — never a `Task`. The workflow engine translates. This keeps the runtime
  testable without a graph and the scheduler testable without an agent.
- **`workflow` and `builder`.** A build reaches the engine by satisfying
  `GateRunner`, not by being imported.

The dashboard is the strictest case: it consumes the HTTP API and imports nothing
from `core`, `task`, `workflow`, or `builder`. A test enforces it.

---

## 2. The three ideas the codebase rests on

### The event log is authoritative

Every state change is an append-only event. The SQL tables are a materialized
view maintained in the same transaction; `RunStore.verify()` compares the two,
and if they disagree the log wins and the tables are rebuilt.

`core/projection.py` is a **pure fold** from events to `RunState` — no database,
no clock, no filesystem. That is why recovery is replay rather than guesswork,
and why a `kill -9` at any instant is survivable. Two deliberate asymmetries: an
unknown event type is *ignored*, so a log written by a newer build still replays
on an older one; a structurally impossible event *raises*, because that means
damage and continuing would produce a confidently wrong answer.

Durability is SQLite with WAL and `synchronous=FULL`, with append-only enforced
by database triggers rather than caller discipline.

### The scheduler is a pure function

`task/scheduler.py` takes a graph and a state and returns a decision. No I/O, no
clock, no randomness. Everything that actually happens — starting attempts,
waiting, backing off, timing out — is in `task/dispatcher.py`. That split is what
makes ordering property-testable over randomly generated DAGs.

### Failure and brokenness are different

A gate that **failed** is a verdict about the code. A gate that **errored** is a
verdict about the harness. The distinction runs through gates, builds, and the
UI (red vs amber). It exists because feedback saying "tests failed" when the test
runner is broken sends the next attempt to edit tests that were fine.

Do not collapse these anywhere, ever.

---

## 3. Execution path

```
POST /workflows/{name}/runs
  │
  ▼
api/routes.py ──▶ AppState.require_executor()   ◀── set by cli.py::cmd_serve (3f59746)
  │                                                 — the only composition root
  ▼
workflow/engine.py  WorkflowEngine.run(definition, executor_factory)
  │   compiles WorkflowDefinition → WorkflowPlan → TaskGraph
  ▼
task/dispatcher.py  concurrency slots, state transitions, backoff, cancellation
  │
  ▼
workflow/executor.py  StepExecutor.__call__(task, attempt)
  │   condition → workspace → agent → commit → gates → merge → outcome
  ├── git_manager/workspace.py   worktree per attempt        (optional)
  ├── agent/runtime.py           AgentRuntime.run(spec, ctx, ledger)
  │     └── provider/…           ModelPort.complete()/.stream()
  ├── git_manager/commit.py      stage + commit               (optional)
  ├── test_runner/runner.py      GateRunner → Verdict         (optional)
  └── git_manager/merge.py       merge to integration         (optional)
        │
        ▼
   core/run_store.py  event + materialized rows, one transaction
        │
        ▼
   api/streaming.py   SSE and WebSocket subscribers
```

`ExecutorFactory` (`api/state.py:61`) is
`Callable[[WorkflowPlan, RunId, EventEmitter], TaskExecutor]`; `create_app`
accepts one (`api/app.py:120`) and `cmd_serve` now supplies it, verified by
`tests/e2e/test_serve_execution.py`.

**All four stages marked *(optional)* above are supplied — conditionally on
`cmd_serve`'s mode.** `serve --repo <git repo>` builds a full
`ExecutionServices` with a `WorkspaceManager`, `CommitManager`, `MergeManager`,
and the `[gates]` suite as a `GateRunner`:

```
condition → worktree per attempt → agent → commit
          → [gates] suite gates acceptance → serialized merge to
            orchestrator/<run>/integration → outcome
```

Both of the architecture's own principles hold in this mode. *Isolation is
not optional* — each attempt gets its own worktree, so concurrency above 1 is
safe, and merges to the integration branch are serialized behind a lock
(`docs/020_ARCHITECTURE.md` §4 rule 1, implemented). *The agent never accepts
its own work* — the gate is the target project's own test suite, not the
agent's say-so.

**`serve` without `--repo` still runs, in `fallback` mode**, which is where
the old description above still applies: `ExecutionServices(fallback_root=…)`
only, a shared plain directory, nothing committed or verified, and
`max_concurrency` clamped to 1 server-side so the shared directory is never
entered by two attempts at once. `execution_mode` in the dry-run report and
`GET /config` say which mode a running server is in. Verified end to end,
concurrently, in both modes by `tests/e2e/test_serve_execution.py`
(`TestServedFullPipeline`, `TestFallbackMode`), and against a real repository
by eight runs against a real desktop application.

---

## 4. Seams — the extension points that exist

These are the ways to add capability without touching the core. Prefer them.

| Seam | Contract | Where |
|---|---|---|
| **`ModelPort`** | `complete` / `stream` / `count_tokens` over neutral types | `provider/base.py:563` |
| **Provider registry** | `(domain, role) → instance`, explicit factory registration | `provider/registry.py:67,116` |
| **`GateRunner`** | `run(cwd, spec) -> Verdict`. Anything that turns a directory into a verdict | `test_runner/base.py` |
| **`TaskExecutor`** | `async (Task, attempt) -> AttemptOutcome`. The dispatcher's only view of work | `task/dispatcher.py` |
| **`ExecutorFactory`** | `(WorkflowPlan, RunId, EventEmitter) -> TaskExecutor`. The composition seam | `api/state.py:61` |
| **`ExecutionServices`** | Every collaborator optional: no `workspaces` → run in `fallback_root`; no `gates` → nothing verified, and the outcome says so | `workflow/executor.py:59` |
| **`ToolRegistry`** | Register a `Tool`; the runtime dispatches by name | `agent/tools.py:240` |
| **`Condition`** | `evaluate` / `describe` / `referenced_steps` | `workflow/definition.py:89` |
| **Event sink** | Optional emitter; the dispatcher stays independent of the store | `workflow/progress.py:198` |
| **Transport injection** | Every provider takes a transport, which is how the whole suite runs offline | `provider/claude.py` |

**`ExecutionServices` deserves emphasis.** Its optionality is what lets this
engine drive work that is not a Git repository change: a plain directory, no
commits, no test suite, and a domain-specific `GateRunner` instead. That is the
shape a downstream project's AI tasks need, and it already exists in the code —
just not above the executor.

---

## 5. Seams that must be built

Each is domain-neutral and justified without reference to any product.

| # | Seam | Why | Blocks |
|---|---|---|---|
| **S1** | ~~An executor factory in `cmd_serve`~~ — **done in `3f59746`**; ~~supply `WorkspaceManager`, `CommitManager`, `MergeManager`, and a gate~~ — **done** (OP-002); ~~call `registry.startup_all()`~~ — **done**. Not done, and judged unnecessary (A.4 in `ROADMAP.md`): lifting the assembly into a separate `composition` module — it lives as module-level helpers in `cli.py`, which tests drive directly | Isolation, verification, and fail-fast health are the difference between running agents and orchestrating verified change | **Closed.** L2 in practice, met |
| **S2** | `provider/claude_cli.py` — a `ModelPort` over the `claude` CLI | A subscription-authenticated backend with no API key. Declares `USAGE_REPORTING` (the JSON envelope carries measured usage) but **not** `TOKEN_COUNTING` (which promises a pre-flight exact count the CLI cannot give), and no `COST_REPORTING`, `PROMPT_CACHING`, or `REFUSAL_SEMANTICS` | subscription-billed work |
| **S3** | Gate registry — a gate **name** resolves to a `GateRunner` supplied by configuration or an embedder | `gates: [tests]` currently only reaches the built-in test runner. `GateRunner` is a Protocol nothing outside can register | domain-specific gates |
| **S4** | Artifacts — a manifest convention plus `GET /runs/{id}/tasks/{t}/artifacts` | A non-Git task produces files, not a diff. Today the only way out is scraping a workspace or a transcript | non-repo work |
| **S5** | `repo_path` optional; `workspace_mode = git \| plain` | Makes `ExecutionServices`' existing workspace-less mode first-class instead of test-only | non-repo work |
| **S6** | `orchestrator/embed.py` — one-shot: prompt + tools + workspace → result + artifacts | Not every caller wants a DAG, a run record, and a stream | embedding |
| **S7** | Budget degradation when a provider declares no token accounting | The token axis is meaningless against a CLI. Wall-clock and tool-call axes must bind, and the API must say tokens are *unmeasured*, not zero | S2 |
| **S8** | Retention for worktrees and transcripts | Q5. Continuous development accumulates both without limit | long-running autonomy |
| **S9** | Per-task and per-run cost ceilings | Per-attempt budgets cannot bound an overnight run | unattended autonomy |
| **S10** | Scheduled and continuous execution | Nothing here is scheduled today, by deliberate choice. Continuous development changes that calculus | Hermes |
| **S11** | Cross-run project memory | Each run starts from the repository and its prompt. An engine developing one product for months should accumulate more | Hermes |

S1 is closed (above). S2 is closed (M18, `provider/claude_cli.py`). S3–S7 are
sequenced in `ROADMAP.md` (mostly Phase C). S8–S11 are `AUTONOMY_ROADMAP.md`.

---

## 6. The trust boundary

```
┌──────────── trusted ─────────────┐   ┌──── untrusted ────┐
│ workflow · task · git_manager    │   │ model output      │
│ core · test_runner · provider    │   │ agent tool calls  │
└───────────────┬──────────────────┘   │ HTTP request body │
                │                      │ workflow YAML     │
                └──── agent/tools ─────┴───────┬───────────┘
                      validate · confine · allowlist
```

- Paths are canonicalized and containment-checked **on the resolved path**, so
  `..`, a symlink pointing out, and an absolute path elsewhere are all rejected
  (`agent/tools.py:90`).
- `run_command` uses an executable **allowlist** and rejects shell metacharacters
  (`agent/tools.py:119`). A blocklist cannot be made complete.
- A path arriving over HTTP is exactly as untrusted as one from a model
  (`api/state.py:300`).
- A workflow file may have been written by an agent: `yaml.safe_load`, never
  `load`.
- Credentials never enter a prompt, a transcript, or a log; the config layer
  rejects secret-looking keys at load time.
- Every destructive Git operation passes one audited chokepoint and refuses
  branches OrchestratorPro did not create.

This is not a statement about agent intent. A harness that assumes well-formed
output breaks the first time output is malformed, and at fleet scale that is
every run.

---

## 7. Prompt assembly and caching

Caching is a prefix match: any byte change invalidates everything after it.
Assembly is ordered by stability, most stable first.

```
[ tools            ] frozen per adapter, sorted by name
[ system prompt    ] frozen per role — no timestamps, no run ids   ◀ cache breakpoint
[ repo context     ] stable per run
[ task spec        ] stable per task
[ attempt feedback ] varies per attempt
[ conversation     ] varies per turn                               ◀ cache breakpoint
```

`agent/prompt.py` **rejects volatile content in the cached prefix** at
construction rather than quietly costing money, and a test renders a prompt twice
and compares hashes. Retry feedback goes in the message, never the prefix.

Providers without prefix caching must not declare `PROMPT_CACHING`. The ordering
stays anyway — it is free, and correct the moment a caching provider is selected.

---

## 8. Rules for change

1. **A module imports from its own layer and below.** A layering violation is a
   review reject, and two tests enforce the parts that matter most.
2. **Only `provider/` names a vendor.** No vendor SDK import, no named binary, no
   vendor conditional anywhere else.
3. **Neutral types only across a port.** A leaked vendor type makes the port a
   lie.
4. **Declare capabilities honestly.** Never claim one you lack; never silently
   no-op one you lack. Silent degradation is the most expensive failure mode in a
   swappable-backend system.
5. **`failed` ≠ `errored`.** Everywhere.
6. **The agent never accepts its own work.** It reports; the gate decides.
7. **Never merge to the default branch.** The human decides what merges.
8. **The engine stays domain-neutral.** Nothing here may know what a VLAN is.
9. **Type everything; `mypy --strict` passes. No `print`. Async-first** — a
   blocking call on the event loop is a bug.
10. **The spec wins; if the spec is wrong, amend it in the same commit.**
