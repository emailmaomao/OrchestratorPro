# 010 — Requirements

**Status:** Approved for M0; reverified 2026-07-31 against the build at M18
**Last updated:** 2026-07-31

Requirement IDs are stable. Never renumber; mark superseded requirements as such
and add a new ID. Every requirement names the milestone that satisfies it and
states how it is verified — a requirement with no verification is a wish.

**Priority:** `MUST` = v1 blocking · `SHOULD` = v1 target, may slip ·
`MAY` = post-v1.

---

## Status at the v1.0 candidate

Every `MUST` has a test that would fail if the behaviour were removed. Four
requirements are met in a shape worth stating precisely, because the table's one
line each does not carry it:

- **FR-1.4** — a generated plan is *returned*, never executed. Running it is a
  separate call, so "unless `auto_approve`" is structural rather than a flag: a
  plan cannot start itself. Task-level approval gates (FR-4.6) are the separate
  mechanism for holding work mid-run.
- **FR-1.2** — verified literally: `tests/planner/test_integration.py` builds a
  graph from a YAML file and from a generated plan and asserts the two are equal
  node for node and edge for edge, then executes both.
- **FR-6.x** — the API is authenticated as of M13, with three roles. With no
  account bootstrapped it serves as an operator, and says so on every start.
- **FR-7.x** — the dashboard covers runs, graphs, builds, events, metrics, and
  system status. **It has no approval page**; the queue, transcripts, and diffs
  are reachable over HTTP only. That is the one deliberate shortfall.

Retention (worktrees and transcripts) remains unbuilt — see Q5 in the spec.

---

## 1. Functional requirements

### FR-1 — Goal intake and planning

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| FR-1.1 | MUST | Accept a goal (free text) plus a repository path and produce a `TaskGraph`. | M9 | Unit: goal → graph with a fake provider. |
| FR-1.2 | MUST | Accept a hand-written YAML workflow that produces an equivalent `TaskGraph`, validated through the same code path as a generated plan. | M9 | Unit: YAML and generated plan yield equal graphs. |
| FR-1.3 | MUST | Reject a cyclic graph at construction, before any work starts. | M3 | Property test over random graphs. |
| FR-1.4 | MUST | Present a generated plan for human approval before execution unless `auto_approve` is set. | M9 | Integration: run halts pending approval. |
| FR-1.5 | SHOULD | Report a planning failure (malformed structured output, refusal) as a typed error naming the cause. | M9 | Unit with a scripted bad response. |

### FR-2 — Execution

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| FR-2.1 | MUST | Execute independent tasks concurrently up to a configured global cap. | M7 | Property test: cap never exceeded. |
| FR-2.2 | MUST | Never start a task before all its dependencies have succeeded. | M3 | Property test over random DAGs. |
| FR-2.3 | MUST | Mark a task `blocked` when any dependency reaches a terminal failure, without attempting it. | M3 | Unit: state machine. |
| FR-2.4 | MUST | Retry a failed task up to `max_attempts`, each retry in a fresh workspace. | M7 | Integration: forced failure then success. |
| FR-2.5 | MUST | Include the previous attempt's gate output in the next attempt's prompt. | M7 | Unit: assembled prompt contains the failure detail. |
| FR-2.6 | MUST | Enforce per-attempt budgets on wall-clock, total tokens, and tool calls; whichever binds first terminates the attempt as `budget_exhausted`. | M6 | Unit, one test per axis. |
| FR-2.7 | MUST | Preserve partial work from a budget-exhausted or failed attempt for inspection. | M6 | Integration: worktree survives failure. |
| FR-2.8 | SHOULD | Support per-label concurrency caps in addition to the global cap. | M7 | Unit. |
| FR-2.9 | MUST | Support cancellation: stop starting new attempts, allow in-flight ones to finish or be killed on request. | M7 | Integration. |

### FR-3 — Isolation

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| FR-3.1 | MUST | Give every attempt its own Git worktree on its own branch. No two attempts share a working tree, ever. | M4 | Integration with concurrent attempts. |
| FR-3.2 | MUST | Never modify the operator's checked-out working tree or current branch. | M4 | Integration: assert tree unchanged after a run. |
| FR-3.3 | MUST | Name branches `orchestrator/<run>/<task>/<attempt>`. | M4 | Unit. |
| FR-3.4 | MUST | Refuse destructive operations on branches OrchestratorPro did not create. | M4 | Unit: guard rejects a foreign branch. |
| FR-3.5 | MUST | Detect merge conflicts and surface them as a structured task failure listing conflicted paths, leaving no half-merged index. | M4 | Integration with a constructed conflict. |
| FR-3.6 | MUST | Never merge to the repository's default branch. | M4 | Integration: default branch unchanged. |
| FR-3.7 | MUST | Verify the repository is clean before a run starts, and refuse otherwise. | M4 | Unit. |

### FR-4 — Gates

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| FR-4.1 | MUST | Run the configured test command in the attempt's workspace before accepting its work. | M5 | Integration. |
| FR-4.2 | MUST | Block acceptance on a failing gate. | M5 | Integration: red suite → task not merged. |
| FR-4.3 | MUST | Report failing test names and captured output in the verdict. | M5 | Unit against a fixture project. |
| FR-4.4 | MUST | Distinguish `errored` (the runner itself broke) from `failed` (tests are red), and never report the former as the latter. | M5 | Unit: missing binary → `errored`. |
| FR-4.5 | MUST | Enforce a gate timeout, terminating the process group on expiry. | M5 | Unit with a hanging fixture. |
| FR-4.6 | SHOULD | Support human-approval gates on configurable task labels. | M9 | Integration via the API. |
| FR-4.7 | SHOULD | Ship pytest, JUnit-XML, and exit-code parsers. | M5 | Unit per parser. |

### FR-5 — Persistence and observability

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| FR-5.1 | MUST | Persist every run, task, attempt, gate result, and approval durably. | M1 | Unit round-trip. |
| FR-5.2 | MUST | Maintain an append-only event log sufficient to reconstruct all run state. | M1 | Unit: replay equals materialized state. |
| FR-5.3 | MUST | Capture a full transcript per attempt — messages, tool calls, tool results — as JSONL on disk. | M6 | Integration. |
| FR-5.4 | MUST | Attribute input and output tokens, and USD cost, to each attempt **where the backend reports them**; where it does not (`claude_cli`, M18), attribute measured-or-estimated tokens labelled `tokens_estimated`, and a `notional_cost_usd` distinct from `cost_usd`, which stays `null` rather than `0.00`. | M2, extended M18 (OP-004/OP-014) | Unit against recorded usage. |
| FR-5.5 | MUST | Resume an interrupted run without redoing completed tasks. | M7 | Integration: kill -9 then restart. |
| FR-5.6 | MUST | Reconcile persisted state against on-disk worktrees on resume, reporting drift. | M7 | Integration: delete a worktree, then resume. |

### FR-6 — Interfaces

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| FR-6.1 | MUST | Expose REST endpoints to create, inspect, and cancel runs, and to resolve approvals. | M9 | API tests. |
| FR-6.2 | MUST | Expose a WebSocket stream of run events. | M9 | API test: event received within the run. |
| FR-6.3 | MUST | Serve a dashboard showing run progress, the task graph, per-task diffs, and cost. | M10 | Manual acceptance against the checklist. |
| FR-6.4 | SHOULD | Provide a CLI covering run, status, cancel, and approve. | M11 | Unit. |
| FR-6.5 | MUST | Generate OpenAPI documentation for every endpoint. | M9 | Schema snapshot test. |

### FR-7 — Providers and adapters

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| FR-7.1 | MUST | Route all model calls through the `Provider` protocol; no vendor SDK import above the provider layer. | M2 | Static check: import-layering test. |
| FR-7.2 | MUST | Support per-role model and effort configuration (planner, worker, summarizer). | M2 | Unit. |
| FR-7.3 | MUST | Surface a refusal (`stop_reason == "refusal"`) as a typed result, never as an exception or a crash on `content[0]`. | M2 | Unit with a recorded refusal. |
| FR-7.4 | MUST | Opt into server-side fallback by default so a policy decline is re-served rather than failing the attempt. | M2 | Unit: request carries the fallback parameter and beta header. |
| FR-7.5 | MUST | Support pluggable adapters, with the tool loop as default. | M6 | Unit. |
| FR-7.6 | SHOULD | ~~Provide an Agent SDK adapter, isolated to a single module.~~ **Not built, not planned.** No `agent_sdk` module exists anywhere in the codebase. The Claude Agent SDK and the Claude Code CLI (FR-7.7) are different integration surfaces; the CLI is the one that shipped. | M6 | — |
| FR-7.7 | MAY | Provide a subprocess adapter for external agent CLIs. | ~~M11~~ **M18** — `adapter/claude_code.py`, driving the Claude Code CLI over `subprocess` | Unit + `tests/adapter/test_claude_code.py`; exercised by eight real runs against a real desktop application. |

---

## 2. Non-functional requirements

### NFR-1 — Correctness

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| NFR-1.1 | MUST | The scheduler performs no I/O and is a pure function of graph and state. | M3 | Review + unit: no I/O imports in the module. |
| NFR-1.2 | MUST | Given identical graph and agent outputs, scheduling decisions are identical. | M3 | Property test with a seeded fake. |
| NFR-1.3 | MUST | Illegal task state transitions raise rather than silently correcting. | M3 | Unit over the transition matrix. |
| NFR-1.4 | MUST | Never silently truncate content that exceeds a context window; report or compact explicitly. | M2 | Unit: oversized input raises a typed error. |
| NFR-1.5 | MUST | Prompt prefixes are byte-deterministic across renders with identical inputs. | M6 | Unit: render twice, compare hashes. |

### NFR-2 — Resilience

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| NFR-2.1 | MUST | A `SIGKILL` at any point leaves recoverable on-disk state. | M7 | Integration: kill at three distinct phases. |
| NFR-2.2 | MUST | Retry transient provider errors (429, 5xx, connection) with exponential backoff. | M2 | Unit with a flaky fake. |
| NFR-2.3 | MUST | A single task's failure never aborts the whole run; unaffected branches continue. | M7 | Integration. |
| NFR-2.4 | MUST | Every custom exception carries a stable `code` and a `retryable` flag. | M1 | Unit over the taxonomy. |

### NFR-3 — Security

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| NFR-3.1 | MUST | Canonicalize every model-supplied path and confine it to the workspace root before any I/O. | M6 | Unit: `..`, symlink escape, outside-root absolute all rejected. |
| NFR-3.2 | MUST | Gate `run_command` on an executable allowlist and reject shell metacharacters. A blocklist is insufficient. | M6 | Unit per rejected form. |
| NFR-3.3 | MUST | Never place credentials in prompts, messages, or transcripts. | M6 | Unit: transcript scanned for configured secret values. |
| NFR-3.4 | MUST | Bind the API to localhost by default; refuse a non-local bind without an auth token. | M9 | Unit: startup fails on `0.0.0.0` with no token. |
| NFR-3.5 | MUST | Read credentials only from the environment or an agent auth profile, never from a config file. | M1 | Unit: a key in TOML is rejected. |
| NFR-3.6 | MUST | Funnel destructive Git operations through one audited function. | M4 | Review + unit. |

### NFR-4 — Performance and cost

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| NFR-4.1 | SHOULD | Scheduler overhead stays under 50 ms per decision for graphs up to 500 tasks. | M3 | Benchmark test. |
| NFR-4.2 | SHOULD | Workspace creation completes within 2 s on a repository of 10 000 files. | M4 | Benchmark test. |
| NFR-4.3 | MUST | Place prompt cache breakpoints so that a repeated prefix produces a nonzero cache read. | M2 | Unit against recorded usage. |
| NFR-4.4 | MUST | Report per-run cost within 1% of provider-reported usage **where the backend reports usage**; where it declares no `COST_REPORTING` (`claude_cli`), report `cost_usd: null` plus a labelled `notional_cost_usd`, never a guessed dollar figure. | M2, carve-out M18 | Unit against recorded usage totals. |

### NFR-5 — Maintainability

| ID | Priority | Requirement | Milestone | Verified by |
|---|---|---|---|---|
| NFR-5.1 | MUST | `mypy --strict` passes with no errors. **Not met.** 46 errors in 14 files, reverified 2026-07-31; tracked as OP-008. Per-line detail in `TECH_DEBT.md`. | M1 onward | Manual run — see note below. |
| NFR-5.2 | MUST | `ruff check` and `ruff format --check` pass. **Met**, reverified 2026-07-31. | M1 onward | Manual run — see note below. |
| NFR-5.3 | MUST | Module layering is enforced: a module imports only from its own layer and below. | M1 onward | Automated import-layering test. |
| NFR-5.4 | MUST | The default test suite makes no live API calls; live tests are marked and optional. | M2 onward | Marker check, run manually — see note below. |
| NFR-5.5 | MUST | `pytest -q` is green before any milestone is marked complete. | Every | Milestone exit criteria. |

**"CI step" above is aspirational.** No CI configuration exists anywhere in
this repository (no `.github/workflows/`, no equivalent) — every "Verified
by: CI step" in this table describes a check that must currently be run by
hand, per `CLAUDE.md`'s milestone checklist, not one a pipeline enforces
continuously. NFR-5.5's "Milestone exit criteria" is the only row that
accurately describes how verification actually happens today.

---

## 3. Traceability

| Milestone | Requirements satisfied |
|---|---|
| M1 | FR-5.1, FR-5.2, NFR-2.4, NFR-3.5, NFR-5.2–5.3 (NFR-5.1 still open — OP-008) |
| M2 | FR-5.4, FR-7.1–7.4, NFR-1.4, NFR-2.2, NFR-4.3, NFR-4.4, NFR-5.4 |
| M3 | FR-1.3, FR-2.2, FR-2.3, NFR-1.1–1.3, NFR-4.1 |
| M4 | FR-3.1–3.7, NFR-3.6, NFR-4.2 |
| M5 | FR-4.1–4.5, FR-4.7 |
| M6 | FR-2.6, FR-2.7, FR-5.3, FR-7.5, NFR-1.5, NFR-3.1–3.3 (**not** FR-7.6 — never built) |
| M7 | FR-2.1, FR-2.4, FR-2.5, FR-2.8, FR-2.9, FR-5.5, FR-5.6, NFR-2.1, NFR-2.3 |
| M9 | FR-1.1, FR-1.2, FR-1.4, FR-1.5 |
| M9 | FR-4.6, FR-6.1, FR-6.2, FR-6.5, NFR-3.4 |
| M10 | FR-6.3 |
| M11 | FR-6.4 |
| M18 | FR-7.7 (moved from its original M11 slot — see FR-7.7's own row) |

**This table stops at M11 and was never extended through M18.** M12–M18
satisfied real requirements (auth/FR-6.x, approvals, retention/Q5's runs half,
e2e, docs, and the two rows above) without a traceability entry recording it —
a gap in this table, not evidence the work wasn't done; see `TASKS.md` for
what M12–M18 actually shipped.

Every `MUST` maps to a milestone. If a milestone ships without satisfying its
listed requirements, it is not complete — the requirement moves, or the
milestone does not close.
