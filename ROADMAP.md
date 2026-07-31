# ROADMAP — OrchestratorPro

**Canonical roadmap.** `TASKS.md` remains the milestone record for M0–M18 (all
complete — M17 and M18 both landed after this file's phases were first drafted,
and are Phase A and Phase B below respectively, now closed); this file is what
comes after, under the strategy in which OrchestratorPro is the permanent
engine that develops a downstream desktop application, with Hermes above it.

Revised 2026-07-31. Ordered by dependency, then by value. No dates.

---

## The rules that order everything

> **The engine stays domain-neutral.** Nothing in `orchestrator/` may know what a
> VLAN is. If a change can only be justified by reference to a product, it
> belongs in that product's repository.
>
> **Orchestration is built here once.** Retries, budgets, approvals, history,
> planning, scheduling, memory — built here, exposed through a stable API,
> consumed downstream. Never duplicated.
>
> **Design for autonomy.** For every subsystem, ask whether Hermes could
> eventually drive it without a human. If yes, build the seam now. If no, write
> down why in `AUTONOMY_ROADMAP.md`.

---

## Phase A — Ignition  ✅ **done**

The engine does not start. Everything else waits.

| # | Item | Seam | Size | Status |
|---|---|---|---|---|
| A.1 | `cmd_serve` builds a registry, runtime, and executor factory | S1 | M | ✅ `3f59746` |
| A.2 | A test that drives the real `cmd_serve` path and runs a workflow to completion | S1 | S | ✅ `tests/e2e/test_serve_execution.py` |
| A.3 | Supply `WorkspaceManager`, `CommitManager`, `MergeManager`, and gates; clamp `max_concurrency` without isolation | S1 | M | ✅ `20742d1` |
| A.4 | Assembly importable and testable (module-level helpers in `cli.py`; a separate `composition` module stays optional) | S1 | S | ✅ |
| A.5 | `registry.startup_all()` + health before the first task; `serve --verify` opts into refusal | — | S | ✅ |
| A.6 | Transcript sink — FR-5.3's JSONL per attempt | — | S | ✅ `59e2f0c` |
| A.7 | `docs/140_COMPOSITION.md` — how an embedder assembles or replaces it | — | S | ⬜ carried |
| A.8 | *(found en route)* Serialize integration merges per `docs/020` §4 rule 1; crash detail on `task.failed` | — | M | ✅ `7a35035` |

**Exit: met.** `orchestratorpro serve --repo <git repo>`, then
`POST /workflows/{name}/runs`, executes each task in its own worktree, gates it
on the project's own suite, merges what passes to the run's integration branch,
and leaves a JSONL transcript — verified end to end, concurrently, by
`tests/e2e/test_serve_execution.py`. **Phase A is done** (A.7 is a docs
deliverable carried into the backlog).

---

## Phase B — Subscription-billed backends  ✅ **done** (M18)

The first product this engine develops is billed by subscription, not by token.
That is a legitimate class of backend, not a special case.

| # | Item | Seam | Size | Status |
|---|---|---|---|---|
| B.1 | `provider/claude_cli.py` — a `ModelPort` driving the `claude` CLI. Probes `--version` at `startup()`; passes the prompt as one argument (a piped stdin hangs behind the Windows `.cmd` shim); maps OAuth expiry to `AUTH_FAILED`, non-retryable | S2 | M | ✅ `7614f53` |
| B.2 | Declare capabilities **honestly**: no `TOKEN_COUNTING`, no `COST_REPORTING`, no `PROMPT_CACHING`, no `REFUSAL_SEMANTICS` | S2 | S | ✅ `7614f53`; superseded in part by B.3/OP-014 — measured tokens now ship where the CLI's own envelope supports it |
| B.3 | Budget degradation: when a provider declares no token accounting, wall-clock and tool-call axes bind and the API reports tokens as *unmeasured*, never `0` | S7 | S | ✅ OP-004 (`8820e2e`, `5232755`, `ea8b40e`); superseded — OP-014 (`3328e21`) went further and reports **measured** tokens from the CLI envelope, labelled `tokens_estimated: false`, not just "unmeasured" |
| B.4 | A `live`-marked conformance run against the real CLI | — | S | ✅ — beyond the env-gated `ORCHESTRATORPRO_TEST_CLAUDE_CLI=1` test, eight real runs have executed this backend against a real desktop application, several with real code adopted |

**Exit: met.** A run completes with no API key present anywhere, and the cost
report says "not reported" (`cost_usd: null`) rather than `$0.00` — with a
`notional_cost_usd` alongside it, charged to nobody, so the run still has a
sense of scale.

**Note.** `openai_compat` already covers Ollama, vLLM, LM Studio, and Open WebUI,
so a local Qwen backend needs **configuration only, no code**. Per-role binding
(`planner` / `worker` / `summarizer`) is the mechanism for "expensive model for
correctness, local model for volume".

---

## Phase C — Work that is not a repository change  ← **current**

Not literally next in strict order — `NEXT_TASK.md`'s queue currently mixes
C items (OP-005/006/007), one D item (OP-009, retention), and one G-adjacent
item (OP-008, mypy) — but C is the first phase below with no item done, and
where the highest-value single item (C.3, the gate registry) lives.

`ExecutionServices` already supports a plain directory with no commits and no
test suite (`workflow/executor.py:59`). Nothing above the executor can express
it.

| # | Item | Seam | Size |
|---|---|---|---|
| C.1 | `workspace_mode = git \| plain`; `repo_path` optional on run creation | S5 | M |
| C.2 | Artifact manifest convention + `GET /runs/{id}/tasks/{t}/artifacts` | S4 | M |
| C.3 | **Gate registry** — a gate *name* resolves to a `GateRunner` supplied by configuration or an embedder | S3 | M |
| C.4 | Per-run tool-surface selection; a documented registration path for custom `Tool`s | S6 | M |
| C.5 | `orchestrator/embed.py` — one-shot: prompt + tools + workspace → result + artifacts, no DAG, no run record | S6 | M |

**Exit:** a caller can run an agent against a plain directory, gate it on **its
own** rules, and collect the files it produced — all over HTTP, with the engine
still knowing nothing about the domain.

C.3 is the highest-value item in this phase. Gating AI output on the consuming
project's own correctness rules is what turns "the model wrote something" into
"the model wrote something that passes".

---

## Phase D — Unattended operation

Budgets per attempt cannot bound an overnight run.

| # | Item | Seam | Size |
|---|---|---|---|
| D.1 | Per-task and per-run cost and token ceilings, enforced by the dispatcher | S9 | M |
| D.2 | Retention for worktrees and transcripts — the open Q5, and the gap an operator hits first | S8 | M |
| D.3 | Disk-pressure guard: refuse to start work when the workspace root is near full | — | S |
| D.4 | A run-level circuit breaker: N consecutive failed attempts across a run pauses it and raises an approval rather than burning the budget | — | M |

**Exit:** a run can be started and left alone. It stops on its own terms, and
what it leaves behind is bounded.

---

## Phase E — Continuous development

Today nothing is scheduled, deliberately: "the host already has a scheduler and
this system does not want to be one." Continuous development changes that.

| # | Item | Seam | Size |
|---|---|---|---|
| E.1 | A trigger API: run this workflow on a schedule, on a webhook, or when a prior run finishes | S10 | L |
| E.2 | Run chaining with outcome conditions — the same condition algebra `workflow/definition.py` already has, one level up | S10 | M |
| E.3 | Backpressure: one run at a time per repository; a queued trigger coalesces rather than piling up | — | M |
| E.4 | A second adapter behind the `Adapter` protocol, which is what proves the seam is real | — | L |

**Exit:** Hermes can say "keep developing this product" and the engine keeps
picking work up.

---

## Phase F — Memory

Each run starts from the repository and its prompt. An engine developing one
product for months should know more than that.

| # | Item | Seam | Size |
|---|---|---|---|
| F.1 | A project-memory store: durable facts about a repository across runs — conventions, past decisions, known-fragile modules, what a prior attempt learned | S11 | L |
| F.2 | Memory in the prompt at the right stability tier — after the system prompt, before the task spec, so caching survives | S11 | M |
| F.3 | Write-back with provenance: which run learned it, and when. An unattributed memory is a rumour | — | M |
| F.4 | Decay and contradiction handling: a fact about a file that no longer exists must not outlive it | — | M |

**Exit:** attempt #40 on a project is better-informed than attempt #1, and can
say why.

---

## Phase G — Closing the honest gaps

Carried from `TASKS.md` "past v1.0" and `RELEASE_NOTES.md` "Known limitations".
None blocks the phases above; all are debts.

| # | Item |
|---|---|
| G.1 | Split `core/events.py` into `core/ids.py`, `core/errors.py`, `core/budget.py` — a pure move, outstanding since M1 |
| G.2 | The dashboard approval page — the one place the API is ahead of the UI |
| G.3 | Recursive planning, **only** if the evidence appears: steps consistently too large |
| G.4 | Multi-repository runs, **only** when the single-repo case is genuinely solid |
| G.5 | An OS-keyring `secrets` provider (`docs/030` §5.9 specifies one; only `env` exists) |
| G.6 | ~~Fix `README.md`'s remaining pre-implementation framing beyond the status block~~ — **done**, this pass |

---

## Deliberately not doing

| Item | Why |
|---|---|
| Hosted SaaS | Local-first is a design constraint, not a stage. Hosting changes the security model entirely |
| Our own model | The provider layer exists so this stays someone else's problem |
| Replacing CI | We consume the project's checks. Defining correctness is the project's job |
| Auto-merge to the default branch | The line. A human decides what merges |
| A general workflow engine | The domain is software change. Phase C widens *workspace*, not *domain*: an isolated directory, optionally Git-backed. Nothing more |
| Domain knowledge of any product | If it can only be justified by reference to a product, it belongs there |

---

## Spec amendments this roadmap requires

Per `CLAUDE.md`, the spec is amended in the same commit as the code that
motivates it. Recorded here so they are not forgotten:

| Phase | Document | Amendment |
|---|---|---|
| A | `ORCHESTRATOR_PRO_SPEC.md` §4.1 | ~~Add `orchestrator/composition.py` and its contract~~ — **superseded by A.4**: the module was judged unnecessary: assembly lives as module-level helpers in `cli.py`, which tests drive directly; nothing to amend |
| B | `docs/030_PROVIDER_INTERFACE.md` §5.1 | A CLI-backed model provider and its capability profile — **still owed**: §5 documents a fictional ten-subdirectory `provider/` layout, not the real flat package with `claude_cli.py` in it |
| C | `ORCHESTRATOR_PRO_SPEC.md` §1.1, §6 | A workspace is an isolated directory, **optionally** Git-backed |
| C | `docs/010_REQUIREMENTS.md` | FR for artifacts and for embedder-supplied gates |
| D | `ORCHESTRATOR_PRO_SPEC.md` §13 | Q5 resolved |
| E | `docs/000_PROJECT_VISION.md` §6 | "Nothing is scheduled" is revisited, with the reasoning |
