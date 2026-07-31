# TASKS.md — milestone plan and status

**Updated:** 2026-07-27
**Current milestone:** M17 ✅ **complete** — see the scope notes below for what
was deliberately carried forward
**Milestones M0–M16:** ✅ all complete; that was the v1.0 candidate and shipped
as v1.0.0. M17 is the first work past the original plan, opened because a defect
was found that no milestone had covered — see below.

**How M17 closed, each item verified by command on 2026-07-27:**

1. ~~`provider.startup()` never called~~ — **done.** `_build_execution` runs
   `registry.startup_all()` + a health check before wiring the executor; a
   missing SDK still degrades to `unavailable`. `serve --verify` opts into
   refusing (`EXIT_FAILED`) instead of serving degraded. Landed from
   `stash@{0}` after review proved `main` had none of it; the stash is dropped
   and the stash list is empty.
2. ~~No transcript sink~~ — **done.** `ExecutionServices.transcripts` routes
   every attempt to `<db-dir>/transcripts/<run>/<task>/<attempt>.jsonl`;
   `AgentRuntime.run` gained a per-call `transcript_sink` flushed in a
   `finally` so timeouts still capture entries. Unit + e2e coverage.
3. ~~Flaky under concurrency~~ — **resolved.** Merges were never serialized,
   though `docs/020` §4 rule 1 had required it since M0; `StepExecutor._merge`
   now holds a per-run lock, and `task.failed` carries the crash detail it
   used to drop. Overlap-detector unit test; 20/20 consecutive e2e passes.

**Carried forward, deliberately (none blocks the milestone):**

- A separate `orchestrator/composition.py` module — the assembly lives as
  importable helpers in `cli.py`, which tests drive directly.
- `_build_services` uses `asyncio.run`, so `_build_execution` silently falls
  back when called from a running event loop — fine for the sync CLI, a trap
  for an async embedder; documented in the helper.
- `GET .../transcript` assembles from events and nothing emits `TOOL_CALLED`,
  so the HTTP transcript is shallow; the JSONL on disk is the full record.
- `docs/140_COMPOSITION.md` (ROADMAP A.7) is unwritten.

Update this file in the same commit that completes a milestone. Do not mark a
milestone complete until `pytest -q` is green.

---

## Status at a glance

| ID | Milestone | Status | Depends on |
|---|---|---|---|
| M0 | Specification | ✅ **Complete** | — |
| M1 | Core foundations | ✅ **Complete** | M0 |
| M2 | Provider layer | ✅ **Complete** | M1 |
| M3 | Task model + scheduler | ✅ **Complete** | M1 |
| M4 | Git manager | ✅ **Complete** | M1 |
| M5 | Test runner | ✅ **Complete** | M1 |
| M6 | Agent runtime + tool loop | ✅ **Complete** | M2, M4 |
| M7 | Workflow engine | ✅ **Complete** | M3, M5, M6 |
| M8 | Builder + planner | ✅ **Complete** | M3, M5 |
| M9 | Workflow construction | ✅ **Complete** | M2, M3 |
| M10 | API | ✅ **Complete** | M7, M8 |
| M11 | Dashboard | ✅ **Complete** | M10 |
| M12 | Hardening + packaging | ✅ **Complete** | M11 |
| M13 | Authentication | ✅ **Complete** | M10 |
| M14 | Approvals | ✅ **Complete** | M7, M13 |
| M15 | Retention + end-to-end | ✅ **Complete** | M12 |
| M16 | Documentation | ✅ **Complete** | M15 |

M6 is the first milestone at which the system does something useful end to end.
That is deliberate — everything before it is scaffolding, and scaffolding that
never gets exercised is scaffolding that is wrong.

---

## M0 — Specification ✅

Establish the source of truth before writing code.

- [x] `README.md`
- [x] `ORCHESTRATOR_PRO_SPEC.md`
- [x] `CLAUDE.md`
- [x] `TASKS.md`
- [x] `docs/000_PROJECT_VISION.md`
- [x] `docs/010_REQUIREMENTS.md`
- [x] `docs/020_ARCHITECTURE.md`

**Exit criteria:** documents are internally consistent, the package layout in the
spec matches the directories on disk, and open questions are recorded rather than
silently assumed. ✔

**Notes.** No tests — this milestone produces no code. The repository previously
contained only empty package directories and a set of malformed `{orchestrator*`
folders left by a failed brace-expansion `mkdir`; those were removed.

---

## M1 — Core foundations 🟡

`orchestrator/core/` plus the two pure domain models. Delivered in two slices;
the first has landed.

### Slice 1 — models, configuration, logging ✅

- [x] Error taxonomy — `OrchestratorError` base with stable `code` and
      `retryable` on every subclass
- [x] Identifiers — `RunId`, `TaskId`, `AttemptId`, `EventId`; prefixed,
      validated, and **strictly monotonic** so lexicographic order equals
      creation order even within one millisecond
- [x] `core/config.py` — TOML loading, layered resolution (defaults → user →
      repo), strict validation, `ConfigError` on bad input
- [x] `core/logging.py` — structured JSON logging, context binding via
      `contextvars`, secret redaction
- [x] `core/events.py` — `Event`, `EventType`, deterministic serialization
- [x] `task/model.py` — `Task`, `GateSpec`, `TaskState` and its transition table
- [x] `agent/model.py` — `TaskSpec`, `Attempt`, `AttemptResult`, `TokenUsage`,
      `BudgetLedger`
- [x] Tests — 271 passing

### Slice 2 — persistence ✅

- [x] `core/storage.py` — the six tables from spec §5, versioned migrations,
      explicit transactions, WAL + `synchronous=FULL`, and **append-only
      triggers** so the audit log is protected by the engine rather than by
      caller discipline
- [x] `core/event_store.py` — durable append (including an idempotent
      `append_if_absent` for crash recovery) and ordered reads
- [x] `core/projection.py` — the pure fold from events to `RunState`; the
      authoritative definition of what a run's state means
- [x] `core/run_store.py` — transactional facade writing an event and its
      materialized rows atomically, plus `rebuild()` (replay → rewrite) and
      `verify()` (fast path vs. truth path)
- [x] `core/records.py` — row and projection value objects
- [x] Tests — 351 passing overall (80 new)

**Exit criteria:** met. A run can be created, persisted, killed mid-flight, and
its state fully reconstructed from the event log alone — covered by
`TestCrashRecovery` in `tests/core/test_run_store.py`. `pytest -q` green.

### Carried forward as debt ⬜

Neither blocks M2; both should land before M11.

- [ ] Split the consolidated primitives out of `core/events.py` into
      `core/ids.py`, `core/errors.py`, and `core/budget.py` — a pure move,
      deferred by the five-file cap on slice 1
- [x] **Decide on Pydantic and SQLAlchemy** — resolved in M10. Pydantic is in,
      but only at the HTTP boundary (`orchestrator/api/schemas.py`), which is
      where `CLAUDE.md` always said it belonged. SQLAlchemy is out: the schema is
      six flat tables and stdlib `sqlite3` carries the durability guarantees
      without the dependency. `CLAUDE.md`'s stack table was amended to match, and
      a layering test keeps every layer below the API on the standard library.

---

## M2 — Provider layer ✅

`orchestrator/provider/` — the only package permitted to know a vendor exists.

- [x] `provider/base.py` — the universal substrate (`Provider`, `ProviderContext`,
      `CapabilitySet`, the closed `ErrorCode` taxonomy) and the `ModelPort`
      contract, with every neutral type that crosses a boundary
- [x] `provider/claude.py` — Anthropic Messages API, transport-seamed
- [x] `provider/openai_compat.py` — Ollama / Open WebUI / vLLM / LM Studio
- [x] `provider/hermes.py` — self-hosted Hermes, with optional recovery of
      inline `<tool_call>` blocks
- [x] `provider/registry.py` — `(domain, role)` resolution, explicit factory
      registration, lifecycle across the whole set
- [x] `tests/provider/` — 183 tests, all offline via injected transports

Binding rules (spec §4.1), each with a test that fails if reversed:

- [x] `thinking={"type": "adaptive"}`; `budget_tokens` never sent
- [x] `output_config={"effort": ...}` for depth control
- [x] `disabled` thinking rejected **client-side** at effort `xhigh`/`max`
- [x] `temperature` / `top_p` / `top_k` never sent — and absent from the neutral
      request entirely, so they cannot be passed by accident
- [x] streaming forced whenever `max_tokens` > 16 000
- [x] `stop_reason == "refusal"` surfaced as a **result**, never a crash
- [x] server-side fallback opted in by default
- [x] cache breakpoints placed per spec §9
- [x] assistant prefill rejected at request construction

**Exit criteria:** met. Completions round-trip against scripted transports with
accurate token accounting and cost attribution, and cost is `None` rather than
`0.0` for unpriced models. `pytest -q` green.

**Open question Q6 is answered.** `docs/030` §11 warned that a port validated by
one implementation plus a fake is a hypothesis. There are now three real
implementations across two very different backend shapes, and the differences
showed up where predicted: the self-hosted providers decline to claim effort
levels, prompt caching, refusal semantics, cost reporting, or exact token
counting, and drop neutral `effort` rather than approximating it.

---

## M3 — Task model + scheduler ✅

- [x] `task/model.py` — `Task`, `GateSpec`, `TaskState` and its transition table
      *(landed in M1)*
- [x] `task/graph.py` — `TaskGraph`: cycle detection, missing-dependency and
      duplicate checks at construction; topological order, ancestors and
      descendants, unblock weights, and `layers()` as the parallel-execution plan
- [x] `task/queue.py` — `ReadyQueue`, a totally-ordered backlog of runnable tasks
- [x] `task/scheduler.py` — the pure function: graph + state → decision, with no
      I/O, no clock, and no randomness
- [x] `task/retry.py` — `RetryPolicy`: fixed, linear, and exponential backoff
      with an injected jitter source so delays are reproducible
- [x] `task/dispatcher.py` — the async driver: concurrency slots, state
      transitions, backoff timers, cancellation, and an optional event sink
- [x] `tests/task/` — 241 new tests, including property tests over random DAGs

**Exit criteria:** met, each with a property test over randomly generated graphs:

- [x] no task starts before its dependencies have succeeded (FR-2.2)
- [x] cyclic graphs are rejected at construction (FR-1.3)
- [x] the concurrency cap is never exceeded (FR-2.1)
- [x] the scheduler performs zero I/O and is deterministic (NFR-1.1, NFR-1.2)
- [x] a task whose dependency died is blocked, never attempted (FR-2.3)

`pytest -q` green.

**Design notes.**

- The dispatcher takes an injected `TaskExecutor` returning a neutral
  `AttemptOutcome`, so the `task` package imports neither `agent` nor
  `provider` — enforced by a test that parses the package's import graph. The
  workflow engine (M7) will supply the adapter.
- Backoff runs on a timer *outside* the concurrency slot. A sleeping retry that
  held its slot would quietly halve throughput on a flaky run.
- Events are emitted through an optional sink rather than written directly, so
  the dispatcher stays independent of the store while integrating with M2.

---

## M4 — Git manager ✅

- [x] `git_manager/repo.py` — repository discovery, cleanliness checks, and the
      **single audited chokepoint**: every invocation is classified `allowed`,
      `authorized`, or `forbidden` before it runs. The `guard.py` of the
      original plan lives here, at the one place every command already passes
      through, rather than as a module callers could route around.
- [x] `git_manager/branch.py` — the `orchestrator/<run>/<task>/<attempt>`
      convention, ownership recognition, and deletion that refuses both foreign
      and unmerged branches
- [x] `git_manager/workspace.py` — worktree create/destroy/prune, rooted outside
      the repository so a run cannot litter the operator's checkout
- [x] `git_manager/commit.py` — staging, committing with correlation trailers,
      and diff inspection, always scoped to a workspace
- [x] `git_manager/merge.py` — integration merges and conflict detection
- [x] `tests/git_manager/` — 141 tests: a mocked runner for guards and parsing,
      plus real Git against throwaway temporary repositories

**Exit criteria:** met, each observed rather than asserted in prose:

- [x] concurrent worktrees never interfere (FR-3.1)
- [x] the operator's checked-out tree is never touched (FR-3.2)
- [x] conflicts surface as structured results naming the conflicted paths, with
      no half-merged index left behind (FR-3.5)
- [x] destructive operations refuse branches OrchestratorPro did not create,
      and `force` does not override ownership (FR-3.4, NFR-3.6)
- [x] merging into the default branch is refused (FR-3.6)

`pytest -q` green.

**Design note — merges use plumbing, not checkout.** `merge-tree` computes the
merged tree, `commit-tree` turns it into a commit, and `update-ref` advances the
branch with a compare-and-swap. No working tree is involved at any point, which
makes FR-3.2 and FR-3.5 structural rather than a matter of remembering to clean
up: there is no index to leave half-merged and no checkout to disturb.

---

## M5 — Test runner ✅

- [x] `test_runner/base.py` — `Outcome`, `Verdict`, `CaseResult`, `CoverageReport`,
      `SuiteSpec`, and the `ResultParser`/`GateRunner` protocols
- [x] `test_runner/discovery.py` — framework detection with an explicit
      confidence level, plus test enumeration via `--collect-only`
- [x] `test_runner/execution.py` — subprocess execution that kills the whole
      **process group** on timeout, with a scripted runner for offline use
- [x] `test_runner/parsers.py` — pytest, JUnit XML, and exit-code parsers, plus
      the coverage collector (XML and terminal)
- [x] `test_runner/runner.py` — the verdict engine and multi-gate aggregation
- [x] `tests/test_runner/` — 165 tests, all against mocked test processes

**Exit criteria:** met.

- [x] a red suite yields the failing case names and captured output (FR-4.3)
- [x] a broken runner yields `errored`, distinctly from `failed` (FR-4.4)
- [x] a timeout terminates the process group and yields `timed_out` (FR-4.5)
- [x] only a clean pass clears a gate (FR-4.2)

`pytest -q` green.

**Design notes.**

- **pytest's exit-code table is transcribed explicitly.** 1 is a failure; 2, 3,
  and 4 are harness problems; 5 (nothing collected) is an *error*, because a
  gate that ran zero tests verified nothing and must not read as green.
- **`Verdict.feedback()` says when the harness broke.** An attempt told "tests
  failed" when the interpreter is missing will start editing tests to go green;
  the retry text names the difference explicitly.
- **Coverage absent ≠ coverage zero.** A project without coverage configured
  reports `None`, so a threshold check errors rather than failing a suite that
  was never measured.
- The package sits at layer 1 and takes a plain `Path`, not a `Workspace` — no
  import of `task`, `agent`, or `git_manager`, enforced by a test.

---

## M6 — Agent runtime + tool loop ✅

- [x] `agent/lifecycle.py` — the five-state machine
      (idle → running → waiting_tool → completed / failed) with enforced
      transitions and per-state timing
- [x] `agent/tools.py` — the tool-call interface: registry, dispatch, the
      path-confinement and allowlist guards, and the workspace-scoped built-ins
      (`read_file`, `write_file`, `list_dir`, `finish`)
- [x] `agent/memory.py` — conversation memory and the context manager
- [x] `agent/prompt.py` — the prompt builder, assembled in stability order, with
      a guard that rejects volatile content in the cached prefix
- [x] `agent/runtime.py` — the runtime and its tool-execution loop
- [x] `tests/agent/` — 184 new tests against a fake provider

Security tests, all present and passing:

- [x] path traversal, symlink escape, and outside-root absolutes all rejected
- [x] executable allowlist enforced; shell metacharacters rejected
- [x] budget exhaustion on each of the three axes terminates cleanly with
      partial work preserved (FR-2.6, FR-2.7)
- [x] prompt determinism: identical inputs render byte-identical prefixes, and
      the prefix is unchanged across turns within an attempt

**Exit criteria — partially met, deliberately.** A task now runs start to finish
against a fake provider: tools execute, files are written into the workspace,
budgets bind, and the attempt terminates with a structured result. It does *not*
produce a commit, because committing is not the runtime's job — the `agent`
package takes a plain workspace path and never imports `git_manager`. Wiring
agent → worktree → commit → gate is precisely what the workflow engine does, so
that half of the criterion moves to M7. `pytest -q` green.

**Scope notes.**

- **No separate `adapter/` package.** The plan sketched `adapter/base.py` and
  `adapter/tool_loop.py`; at the five-file cap the loop lives in
  `agent/runtime.py` with its tool surface in `agent/tools.py`. The pluggable
  `Adapter` protocol — for swapping in an external harness — is the `agent`
  domain in `docs/030` §5.2 and is deferred until there is a second harness to
  swap in.
- **`run_command` and `run_tests` are not built in.** Both need the `shell` and
  `build` provider domains (`docs/030` §5.4, §5.3). Adding a second, unguarded
  path to execute things would undo the point of the allowlist, so the guard
  (`check_command`) ships now and the tools wait for their ports.

---

## M7 — Workflow engine ✅

- [x] `workflow/definition.py` — `WorkflowDefinition`, `StepDefinition`, the
      condition algebra, and compilation of names into a `TaskGraph`
- [x] `workflow/progress.py` — progress derived from task states, and the event
      emitter that cannot take a run down with it
- [x] `workflow/recovery.py` — replay a persisted run into a resume plan, and
      reconcile it against the worktrees actually on disk
- [x] `workflow/executor.py` — the step executor:
      condition → worktree → agent → commit → gates → merge → outcome
- [x] `workflow/engine.py` — run and resume, run-level timeout, cancellation
- [x] `tests/workflow/` — 161 new tests, including resume-after-interruption

**Exit criteria — met.** A multi-task graph runs to completion in dependency
order under a concurrency cap; a run cut off mid-flight resumes from the log
without redoing completed work and without emitting duplicate events.
`pytest -q` green: 1426 passing.

**Scope notes.**

- **No `workflow/concurrency.py` or `workflow/retry.py`.** Both already exist:
  concurrency slots and per-label caps live in `task/scheduler.py`, and
  retry-with-backoff in `task/retry.py`. A second scheduler in the workflow
  layer would be a scheduler that can disagree with the first one. The engine
  supplies what genuinely spans a run — a run-level timeout distinct from the
  per-attempt budgets, cancellation, persistence, and progress in step names.
- **`workflow/resume.py` is `workflow/recovery.py`.** Same job, and it does
  reconcile the plan against on-disk worktrees; drift is reported rather than
  repaired, since deleting a worktree an operator is mid-way through inspecting
  would be worse than telling them about it.
- **Two additive changes outside the package.** `task/dispatcher.py` gained
  `initial_states` / `initial_attempts` (so a resumed run starts from persisted
  state rather than re-running completed tasks), `report_now()` (so an abandoned
  run can still be reported after a timeout), and `attempt.started` /
  `attempt.finished` events — replay counts attempts from those, and without
  them a resumed single-attempt step would arrive with its allowance already
  spent.
- **Resuming does not retry work that failed on its own terms.** An interrupted
  attempt produced no outcome and is refunded; a step that exhausted its
  attempts stays terminal unless the caller passes `retry_failed=True`.

---

## M8 — Builder + planner ✅

- [x] `builder/model.py` — build units, artifacts, diagnostics, results, and the
      content-digest primitives everything else is a function of
- [x] `builder/analysis.py` — the project analyzer (what is here) and the
      dependency analyzer (what depends on what), plus `UnitGraph`
- [x] `builder/cache.py` — fingerprints, the in-memory and SQLite caches, and
      artifact tracking
- [x] `builder/planner.py` — incremental planning, rebuild propagation, and
      automatic replanning after a failure
- [x] `builder/runner.py` — the build executor, build-failure analysis, build
      event logging, and `BuildGate`
- [x] `tests/builder/` — 260 new tests, all offline

**Exit criteria — met.** Editing one file rebuilds that unit and everything
downstream of it and nothing else; a second build of unchanged sources runs no
commands; a build whose artifacts were deleted rebuilds rather than claiming
they are there. `pytest -q` green: 1686 passing.

**This milestone was redefined, and the spec was amended with it.**
`M8 — Builder` originally meant the YAML workflow schema and the LLM planner.
The operator's M9 brief specified a build subsystem — project analysis,
incremental planning, build cache, artifact tracking, failure analysis. Both are
real; they are not the same thing. The build subsystem took the `builder/` name
and this slot; declarative workflow construction moves to `M9 — Workflow
construction` below, still carrying FR-1.2. `ORCHESTRATOR_PRO_SPEC.md` §4.1 and
`docs/020_ARCHITECTURE.md` §2 were updated in the same commit.

**Scope notes.**

- **Incremental compilation is supported, not implemented.** A unit declares
  whether its tool can build partially (`incremental`); the planner decides
  *which* units to invoke and the tool decides what to do inside one. Writing an
  incremental compiler for languages we do not own would be a worse version of
  the tool already installed.
- **Integration is by shape, not by import.** The plan becomes a `TaskGraph` and
  runs on the M3 dispatcher; the build reaches the M7 engine by satisfying
  `GateRunner`, so `builder` and `workflow` stay independent siblings. A
  layering test enforces it.
- **Two small edits outside the package.** `core/events.py` gained three build
  event types — replay ignores unknown types, so older logs are unaffected — and
  `workflow/executor.py`'s `ExecutionServices.gates` is now typed to the
  `GateRunner` protocol rather than the concrete `TestRunner`, which is what
  makes a build usable as a gate.
- **The cache owns its own tables.** They are created by `SqliteCache`, not by
  the core migration, so an operator can clear a build cache without touching
  the event log.

---

## M9 — Workflow construction ✅

*Built after M12, out of order, because M12 shipped without it.*

- [x] `planner/schema.py` — the YAML workflow schema, validation that collects
      **every** problem with a path (`steps[2].depends_on[0]`) rather than
      stopping at the first, and the JSON Schema the LLM is constrained by
- [x] `planner/loader.py` — parse and compile into a `WorkflowDefinition`
- [x] `planner/llm.py` — LLM decomposition via structured output, validated
      through the same path as hand-written YAML
- [x] `cli.py` — `workflow check <file>`
- [x] `tests/planner/` — 137 tests, including the equivalence proof

**Exit criteria — met.** `test_integration.py` builds a `TaskGraph` from a
hand-written YAML file and from a generated plan and asserts the two are equal
node for node and edge for edge, then executes both on the M7 engine (FR-1.2).
`pytest -q` green.

**Scope notes.**

- **One dependency crossed the line: PyYAML.** It is the only runtime dependency
  above L0 that is not a web-layer concern. A hand-rolled YAML subset would
  accept documents real YAML rejects, and a workflow file may be agent-written —
  that is the wrong place to be approximately correct. Parsing is
  `yaml.safe_load`, never `load`, for the same reason.
- **A cycle is a load-time error.** `load_workflow` compiles the graph as part of
  loading, so a file that would deadlock is refused by `workflow check` rather
  than at minute forty of a run.
- **A refusal is not retried.** `PlannerRefused` is raised; only a schema
  violation gets a correction turn. Re-asking a model that declined is how a
  planner burns a budget on nothing.
- **Q2 is answered: one level.** The planner decomposes a goal into steps and
  does not recurse.

---

## M10 — API ✅

- [x] `api/schemas.py` — Pydantic request and response models, strict about
      unknown fields
- [x] `api/state.py` — `AppState`, the injected execution seam, path confinement,
      and the exception-to-status mapping
- [x] `api/streaming.py` — the event broker, SSE framing, WebSocket payloads
- [x] `api/routes.py` — runs, tasks, workflows, agents, builds, events
- [x] `api/app.py` — `create_app`, the error envelope, OpenAPI metadata, `serve`
- [x] `tests/api/` — 198 new tests over the real application

**Exit criteria — met.** 22 paths, all documented and tagged; OpenAPI is
generated and every operation carries a summary; the server refuses a
non-loopback bind without a token. `pytest -q` green: 1885 passing.

**Scope notes.**

- **No authentication, by instruction.** `serve()` refuses any non-loopback bind
  outright rather than binding an unauthenticated service (NFR-3.4). There is no
  `api/auth.py` because there is nothing yet for it to do.
- **SSE as well as WebSocket.** The plan named only a socket. A browser watching
  a run wants SSE and a script wants neither to matter, so both transports carry
  the same content. A subscription is opened before the log is replayed, so
  nothing is lost in the seam, and a client that stops reading is disconnected
  with an explicit notice rather than served a silently partial stream.
- **Reads replay; writes append.** `GET /runs/{id}` reconstructs from events
  rather than reading the materialized tables. Amending a task appends a fresh
  declaration and retiring one abandons it — the log is append-only, so `DELETE`
  cannot mean erase.
- **One change outside the package.** A cancelled run no longer also records
  `run.finished` (`workflow/engine.py`). It had made cancel-then-resume — the
  most ordinary recovery there is — impossible, because recovery treats a
  finished run as closed.
- **Dependencies arrived.** FastAPI, Uvicorn, Pydantic, and httpx are installed
  in `.venv` and pinned in `requirements.txt`. Everything below the API layer is
  still standard-library-only, enforced by a test.
- **Not implemented:** attempt history, per-task diffs, and approval resolution.
  All three need read models or the approval flow of spec §6; the spec now says
  so rather than listing them as though they exist.

---

## M11 — Dashboard ✅

- [x] `dashboard/app.py` — the backend: one document and a static directory
- [x] `static/index.html`, `static/assets/css/main.css` — the shell
- [x] `assets/js/api.js` — the only module that touches the network
- [x] `assets/js/format.js`, `graph.js` — the pure derivations and DAG layout
- [x] `assets/js/ui.js` — DOM construction, no markup from strings
- [x] `assets/js/app.js` — router, page lifecycle, one shared event stream
- [x] `assets/js/pages/` — runs, workflows, agents, builds, events, system
- [x] `tests/dashboard/` — 44 pytest tests plus 80 frontend unit tests run
      through Node's built-in runner

**Exit criteria — met.** A run is observable start to finish: the run list, its
task graph, its task table, and its log are all live, updating from the shared
event stream. No build step, no bundler, no package manager. `pytest -q` green:
1930 passing.

**Scope notes.**

- **The five-file cap and "keep the frontend modular" pull in opposite
  directions, and modular won.** The dashboard is 13 files: a backend, a shell,
  a stylesheet, six library modules, and six page modules. Ten features in five
  files would have meant one very large file, which is the opposite of what the
  milestone asked for. The count is stated here rather than quietly exceeded.
- **Two kinds of test.** Node's runner covers the pure modules — the API client,
  the formatters, the graph layout — where a bug is a wrong answer. The pytest
  suite covers the server and the structure of what it ships: the import graph
  resolves, nothing reaches a CDN, no module builds markup from a string,
  network access lives in one file, and every endpoint the client calls exists
  in the API's own document. Node is a development convenience; if it is absent
  those tests skip rather than fail.
- **One endpoint was added to M10.** `GET /runs/{id}/log` reads a run's history
  with paging. Without it, the only way to read a settled log was to open a
  stream and guess when the replay had ended, which is not something worth
  shipping.
- **Transcripts, diffs, and approvals are still absent**, in the UI as in the
  API. They need endpoints M10 did not build; both documents now say so.

**Carried forward.** The `orchestrator/adapter/` package is still an empty
directory, and `orchestrator/core/events.py` still holds the primitives that
were meant to be split. Both are M12 work.

---

## M12 — Hardening + packaging ✅

- [x] `ops/hardening.py` — security headers, body limits, trusted hosts, CORS
      off by default, structured per-request logging with correlation IDs, and
      `verify_deployment`, which refuses an unsafe bind rather than warning
- [x] `ops/ratelimit.py` — a continuously refilling token bucket per client,
      with streaming endpoints exempt
- [x] `ops/backup.py` — online snapshots with a verified manifest, restore that
      moves the old database aside, and count-based retention
- [x] `ops/migrate.py` — versioned configuration migration that previews by
      default and reports what it moved
- [x] `ops/bench.py` — benchmarks for the four things that decide whether a long
      run stays responsive, with loose regression floors
- [x] `cli.py` — `serve`, `config show|check|migrate`,
      `backup create|list|verify|restore|prune`, `bench`, `version`
- [x] `core/config.py` — a `[security]` section, `config_version`, and an
      environment layer (`ORCHESTRATORPRO__API__PORT=8080`)
- [x] `core/logging.py` — free-form correlation values on `bind_context`
- [x] `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`,
      `.dockerignore`, `scripts/install.sh`, `scripts/install.ps1`
- [x] `tests/ops/` — 297 new tests

**Exit criteria — partially met.** The wheel builds from a clean checkout and
carries all thirteen dashboard assets and a working console script; the CLI
runs from it. `pytest -q` green: 2234 passing.

**Not met: there is no worked example against a real repository.** That needs a
model backend, and running one is the operator's decision, not a thing this
milestone could do on their behalf. The `serve --dry-run` path, the deployment
checks, and the benchmarks are all exercised; an end-to-end run against a live
provider is not, and has never been part of the default suite.

**Scope notes.**

- **The file count is well over five.** Six operations modules, a CLI, seven
  packaging artifacts, and two edits to `core`. Ten deliverables spanning
  hardening, packaging, backup, migration, and benchmarking do not compress into
  five files; the count is stated rather than quietly exceeded.
- **Hardening fails closed.** A non-loopback bind is refused unless the
  deployment names its allowed hosts *and* enables rate limiting *and* has a
  token variable — `verify_deployment` raises rather than warns, and `serve`
  exits 3. This build ships no authentication, which makes those the only thing
  standing between a convenience default and an open port.
- **Middleware order is load-bearing and documented.** Security headers are
  outermost so a 429 carries them too; the request log wraps the rate limiter so
  a refused caller is the one you can find afterwards.
- **Two bugs were found by their own tests.** The token bucket treated a
  monotonic clock reading of zero as "never used" and refilled on every call,
  limiting nothing. The correlation header was looked up with a bytes key
  against a string-keyed dict, so a supplied request ID was silently discarded.
- **Retention is only half done.** Backups have count-based pruning. Worktrees
  and transcripts do not, which is Q5 and remains the largest gap.

---

## M13 — Authentication ✅

- [x] `auth/models.py` — `Role`, scrypt password hashing, API-key generation and
      hashing, `User`, `ApiKey`, `Principal`, the error taxonomy
- [x] `auth/tokens.py` — HS256 access and refresh tokens on the standard library
- [x] `auth/store.py` — users, keys, sessions, and an append-only audit log
- [x] `auth/service.py` — login, refresh, logout, authenticate, administration
- [x] `api/security.py` — the FastAPI dependencies (`requires_viewer` /
      `_operator` / `_admin`) and the open-mode fallback
- [x] `api/auth_routes.py` — 13 endpoints under `/auth`
- [x] `cli.py` — `auth bootstrap|users|add|audit`
- [x] `tests/auth/` — 185 tests

**Exit criteria — met.** With no accounts, every route serves as an operator and
the CLI says so loudly on every start. After `auth bootstrap`, every route
requires a credential, roles are enforced per route, and every privileged action
is in the audit log. `pytest -q` green.

**Scope notes.**

- **JWT is hand-rolled on `hmac` and `hashlib`, on purpose.** One algorithm, and
  the header's `alg` is *checked but never used to select a verifier* — that
  inversion is the `alg: none` and RS256-confusion class of bug, and the tests
  are written as attacks rather than as round-trips.
- **The role check is `>=` on an ordered enum**, not set membership. A new role
  slotted between two existing ones does not silently grant everything.
- **Dependencies take `HTTPConnection`, not `Request`.** A `Request`-typed
  dependency cannot be resolved on a WebSocket route, which is how the streaming
  endpoints ended up unauthenticated in the first draft.
- **`?token=` is accepted on `/events` and `/ws` only.** A browser cannot set a
  header on `EventSource` or `WebSocket`; a query token elsewhere would end up in
  access logs for no reason.
- **Authentication is uniform in time and in message.** An unknown user is hashed
  anyway, a disabled account and a wrong password are the same 401, and both are
  audited.
- **`delete_user` refuses the last admin, and `set_role` refuses self-demotion.**
  Both are how an installation locks itself out.

---

## M14 — Approvals ✅

- [x] `workflow/approval.py` — the queue, decisions (approve / reject / retry),
      attempt history with `repeated_failures`, and transcript assembly
- [x] `git_manager/repo.py` — `diff_branch()`, the diff a reviewer reads
- [x] `api/routes.py` — six endpoints: queue, request, resolve, attempts,
      transcript, diff
- [x] `tests/workflow/test_approval.py` — 39 tests

**Exit criteria — met.** A task can be held for review, a reviewer can read the
attempt history, the transcript of what the agent actually did, and the diff it
produced, and approve, reject, or send it back for another attempt.

**Scope notes.**

- **An approval is an event, not a row.** Requesting and resolving both append,
  so the decision, its author, and its timing survive a rebuild. Resolving also
  emits the consequence — `TASK_ABANDONED` on reject, `TASK_READY` on retry — so
  the projection needs no special case.
- **The actor comes from the credential, never from the body.** A reviewer cannot
  record a decision as someone else.
- **Reject abandons; it does not delete.** The record stays.

---

## M15 — Retention + end-to-end ✅

- [x] `ops/retention.py` — policies, gzip archives with a digest, verification by
      digest *and* replay, pruning, and restore
- [x] `cli.py` — `retention plan|apply|verify`
- [x] `tests/ops/test_retention.py` — 52 tests
- [x] `tests/e2e/` — complete workflow execution, restart and recovery,
      multi-agent, approvals, real Git, load (slow), Docker deployment (slow),
      live providers (live)

**Exit criteria — met.** A completed run can be archived, verified, pruned from
the live database, and restored, with the archive proving it replays to the same
state before anything is deleted. `pytest -q` green.

**Scope notes.**

- **Pruning drops the append-only triggers and restores them in the same
  transaction.** There is no other way to delete from a table the engine protects
  from deletion; doing it inside one transaction is what stops a crash mid-prune
  from leaving the log editable. A test deletes a row afterwards and expects to
  be refused.
- **Verify replays, it does not just checksum.** A digest proves the bytes are
  intact; replaying proves they still mean the run they claim to.
- **`apply_retention` is `dry_run=True` by default**, and `describe()` on a
  policy with `archive=False` shouts DELETE WITHOUT ARCHIVING.
- **The Docker test really builds the image and starts a container**, and a
  second test asserts that an unsafe bind exits 3 rather than serving. Both are
  marked `slow`.
- **The live provider tests skip here.** They need
  `ORCHESTRATORPRO_TEST_OPENAI_BASE_URL` or `_HERMES_BASE_URL`; no endpoint or
  credential exists in this environment, so they have never been run. That is
  stated rather than papered over — M12's unmet exit criterion is unchanged.
- **Q5 is only partly answered.** Runs, backups, and sessions have policies.
  Worktrees and transcripts still do not.

---

## M16 — Documentation ✅

- [x] `docs/100_INSTALLATION.md` — install, configure, bootstrap, deploy
- [x] `docs/110_API_GUIDE.md` — every endpoint, with roles and streaming
- [x] `docs/120_USER_MANUAL.md` — describing work, watching it, approvals, costs
- [x] `docs/130_DEVELOPER_GUIDE.md` — the layering and why it is enforced

Each ends with what the build does *not* do. A manual that omits the gaps sends
the reader looking for a feature that is not there.

---

## M17 — The served composition root 🟡

*Opened after v1.0. The milestone plan ended at M16; this is the first work past
it, and it exists because a defect was found that no milestone had covered.*

### Slice 1 — the engine can execute ✅ (`3f59746`)

- [x] `cli.py::cmd_serve` builds a `ProviderRegistry`, an `AgentRuntime`, and a
      `StepExecutor`, and assigns `state.executor_factory` before `create_app`
- [x] The block degrades gracefully: a missing provider SDK leaves a
      recording-only server rather than refusing to start
- [x] `execution_available` added to the dry-run report
- [x] `tests/e2e/test_serve_execution.py` — drives the **real** `cmd_serve` path
      (patching `uvicorn.run` to capture the app, injecting a scripted transport)
      and runs a workflow to completion over HTTP

**Why this was missed for seventeen milestones.** Every layer was tested in
isolation, and every test assembled the system itself — `tests/api/conftest.py`
injects a `RecordingExecutor`. Nothing exercised the production assembly, so
`serve` reported `execution_available: false` and returned 503 on every
execution request. The lesson is recorded in `AUTONOMY_ROADMAP.md` §6: local
correctness everywhere, and no one asking whether the whole thing runs.

**The first test of the fix was not sufficient either.** It asserted
`execution_available` was *present and boolean* in the dry-run report, which
passes whether the value is `true` or `false` and never runs a task. A test that
would pass if the feature were deleted is not a test.

### Slice 2 — isolation and verification ⬜ (**OP-002**)

Not started. The served wiring builds `ExecutionServices(fallback_root=…)` only:

- [x] Worktree per attempt — `serve --repo` builds a `WorkspaceManager` rooted
      under the serve workspace; two concurrent steps get distinct worktrees
      (asserted against real Git)
- [x] Gates — the `[gates]` suite runs as the `tests` gate; a red gate blocks
      acceptance and feeds the retry; fixed en route: `gate.evaluated` was
      emitted with `attempt_id=None`, which the projection refuses, so the
      first verdict poisoned every later replay of its run. The `EventEmitter`
      now attributes verdicts to the open attempt it learns from the
      dispatcher's own events
- [x] Commits and merges — a passing attempt merges to
      `orchestrator/<run>/integration`; the operator's checkout stays clean
- [x] Fallback without a repository stays available, loudly: every downgrade is
      logged with its cause, the dry-run reports `execution_mode`
      (`full | fallback | unavailable`), and fallback clamps
      `run.max_concurrency` to 1 so the shared directory is never parallel
- [x] Assembly is importable and testable — `_build_execution` /
      `_build_services` are module-level in `cli.py` (a separate `composition`
      module remains open below)
- [ ] `registry.startup_all()` — `execution_available: true` deliberately means
      "an executor is wired", not "a model is reachable" (decision recorded in
      `API_STATUS.md` §1); an opt-in `serve --verify` probe is the next slice
- [ ] Transcript sink — FR-5.3's JSONL transcripts are specified and never written
- [ ] A separate `orchestrator/composition.py` module, if the helpers outgrow `cli.py`
- [x] Read settings for the provider actually **bound** — canonical key is now
      `anthropic` with `claude` as a working alias; `settings_for()` is the one
      name→block mapping; regression tests cover TOML, the alias block, the env
      layer, and the wire payload end to end
- [x] `cli.py:349` — the bound provider is narrowed to `ModelPort` before
      reaching `AgentRuntime`; mypy strict baseline 51 → 46, none added

**Exit criteria:** a run in its own worktree, gated on the project's suite, merged
to an integration branch, with a transcript on disk.

### Also found

- **`mypy --strict` does not pass** — 51 errors in 17 files (NFR-5.1,
  `CLAUDE.md`). Neither mypy nor ruff was installed in `.venv`: both are in the
  `dev` extra of `pyproject.toml` and absent from `requirements.txt`, which is
  how the gap survived. Tracked as OP-008.
- `tests/e2e/test_end_to_end.py::TestDockerDeployment::test_the_dockerfile_is_syntactically_valid`
  is **not** marked `slow`, so `-m "not slow"` does not deselect it and it fails
  wherever no Docker daemon is listening.

---

## M18 — Provider expansion (ROADMAP Phase B) ✅

### Slice 1 — the subscription-billed backend ✅ (OP-003)

- [x] `provider/claude_cli.py` — Claude Code (`claude -p … --output-format
      text`) behind the neutral `ModelPort`; no API key exists anywhere
- [x] The design decision recorded in the module and the spec: a one-shot
      **ModelPort** mapping now; the harness mapping (`docs/030` §5.2,
      `EXTERNAL_IO`) is future work with its own confinement conversation
- [x] Capability honesty: no streaming / tool calling / token counting / cost
      reporting / prompt caching / refusal semantics declared; `stream()`
      raises `NOT_SUPPORTED` rather than emulating; usage is estimated with
      `cost_usd = None`
- [x] Invocation rules already paid for once in production: prompt as one `-p` argument (the
      Windows `.cmd` stdin hang), process-group kill on timeout — proven by a
      test against a real child process, not a returned call
- [x] Error taxonomy: missing exe → `UNAVAILABLE` at startup; expired login →
      `AUTH_FAILED`, non-retryable, with a `/login` hint; overrun → `TIMEOUT`
- [x] Registry: registered alongside the others, selectable purely by config
      (`[provider.claude_cli]`), **not** the default; `ProviderConfig` gains
      `executable` / `timeout_s`; regression tests mirror the OP-002 pattern
- [x] e2e: a served run completes through worktree + gate + merge with the CLI
      backend selected by a repo-local TOML, scripted runner injected; one
      env-gated live test (`ORCHESTRATORPRO_TEST_CLAUDE_CLI=1`), never required

**Scope note.** `--model` is never forwarded from configuration: the block's
default is the API model id, and forwarding it would silently override the
model the operator configured in the CLI itself. Opt-in via direct construction
only, until config can distinguish "set" from "defaulted".

### Slice 2 — honest token accounting ✅ (OP-004)

- [x] `Usage.tokens_estimated` → `TokenUsage.estimated` → `AttemptRecord` /
      `UsageTotals`, sticky under aggregation: a total with one estimated
      contribution is an estimate
- [x] The token budget axis **still binds** on estimates — deliberately; an
      unbounded axis under an unmetered backend is how an overnight run runs
      away — but exhaustion says "(token counts are estimates)" and carries
      the flag in its detail
- [x] Observable everywhere: run-detail `usage.tokens_estimated`, per-attempt
      `tokens_estimated` on `GET .../attempts`, `~`-prefixed totals in the
      dashboard, the flag in `attempt.finished` payloads
- [x] **Found and fixed underneath:** the dispatcher's `attempt.finished`
      payload carried only `status`, so the projection read every attempt's
      tokens as zero — the runtime's counts died in the executor and FR-5.4
      was broken end to end in the served path. Usage now rides on every
      outcome and into the payload
- [x] Additive only: replay ignores unknown payload keys, a pre-flag payload
      reads as measured (both tested); no DB migration — the flag lives in
      payloads and the projection, and reads replay the log

**M18 is complete.** Phase B closed; Phase C (the downstream embedding
surface) is next — see `NEXT_TASK.md`.

---

## A note on numbering

The operator's milestone numbers ran one ahead of this file's from M8 onward:
their "M9 — Builder + planner" is M8 here, their "M12 — Hardening" is M12 here
only because this file's M9 was skipped and built later. M13–M16 above are this
file's names for the four workstreams the operator asked for after M12, which
they numbered 2–6 without milestone labels. The mapping is recorded here rather
than resolved, because renumbering finished milestones would break every commit
message that names one.

---

## Open questions

Carried from spec §13. Every one is now answered by a milestone rather than by an
assumption, except the last.

| # | Question | Needed by | Status |
|---|---|---|---|
| Q1 | Are non-Anthropic providers actually required? | M2 | **Resolved** in M2 — three implementations, two backend shapes |
| Q2 | Recursive planning, or one level? | M9 | **Resolved** in M9 — one level, and the planner says so |
| Q3 | Multi-repository runs in scope? | M7 | **Resolved** — no, one repo per run |
| Q4 | Multi-user auth on the dashboard? | M10 | **Resolved** in M13 — yes: three roles, and open mode when no account exists |
| Q5 | Retention policy for worktrees and transcripts? | M12 | **Partly.** Runs, backups, and sessions have policies; worktrees and transcripts do not |

These assumptions were load-bearing. Q5 is the one still open, and it is the
largest remaining gap in the build.

---

## Carried forward past v1.0

Recorded rather than forgotten, and none of it blocks a release:

- **Retention for worktrees and transcripts** (Q5).
- **An end-to-end run against a real model backend.** The live tests exist and
  are marked; no endpoint has ever been supplied, so they have never run.
- **`orchestrator/adapter/` is still an empty package.** The `Adapter` protocol
  waits for a second harness to swap in.
- **`core/events.py` still holds the consolidated primitives** that M1 meant to
  split into `core/ids.py`, `core/errors.py`, and `core/budget.py`.
- **No per-task cost limit**, only per-attempt budgets.
