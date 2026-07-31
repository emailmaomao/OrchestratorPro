# AUTONOMY_ROADMAP — OrchestratorPro

**The question this document answers:** how much of software development can this
engine do without a human, what stands in the way today, and in what order those
obstacles fall.

The test applied to every subsystem: **"Can this eventually become autonomous?"**
If yes, the seam is designed now. If no, the reason is written down here rather
than discovered later.

Revised 2026-07-31.

---

## 1. Levels of autonomy

Borrowed deliberately from vehicle autonomy, because the failure mode is the
same: the dangerous level is the one where the system does almost everything and
the human has stopped paying attention.

| Level | Meaning | Human does | Status |
|---|---|---|---|
| **L0** | Manual | Everything. An agent is a chat window | passed |
| **L1** | Assisted | Runs one agent per task, reviews each diff | ✅ built |
| **L2** | Partial | States a goal; the system decomposes, executes in parallel, gates, and merges to an integration branch. Human reviews the branch | ✅ **built** (M17 slice 2 / OP-002) — worktree per attempt, the project's own `[gates]` suite, serialized merges to `orchestrator/<run>/integration`, verified by eight real runs against a real desktop application |
| **L3** | Conditional | The system runs unattended for hours: retries with feedback, escalates only when stuck, bounded by cost. Human reviews outcomes, not steps | 🟡 partly — needs cost ceilings, retention, a circuit breaker |
| **L4** | High | The system runs continuously: picks the next task from a roadmap, schedules itself, learns across runs, reports what it did. Human sets direction and approves merges | ❌ needs triggers, memory, task selection |
| **L5** | Full | The system decides direction. **Explicitly not a goal** | never |

**L5 is refused on purpose.** `docs/000_PROJECT_VISION.md` §4 fixes the line:
"The human decides what merges." Automating the last step trades the operator's
judgment for throughput, which is the wrong trade at any speed. An engine that
chooses its own objectives has no one to answer to.

**Current position: L2, achieved.** The engine isolates every attempt in its own
worktree, gates it on the target project's own suite, and merges what passes —
verified against a real repository eight times over, not
only against fakes. What is missing is everything L3 needs above the
per-attempt level: cost ceilings, retention, a circuit breaker (§3).

---

## 2. What is manual today, and why

Everything requiring a human right now, with a verdict on each.

| # | Requires a human | Because | Can it be autonomous? |
|---|---|---|---|
| M-1 | ~~Starting the engine at all~~ | Fixed in `3f59746`; verified by `tests/e2e/test_serve_execution.py` | **Done** |
| M-1a | ~~Trusting what a run produced~~ | **Done** — gates, worktree isolation, and commits are wired (OP-002); what remains is a human reviewing the *integration branch*, which is M-5, deliberately | — |
| M-2 | Choosing and configuring a model backend | Config only; the registry resolves it | **Yes**, already |
| M-3 | Writing the workflow, or reviewing a generated plan | FR-1.4: a plan is *returned*, never self-executed | **Partly.** Keep human approval for a *new* goal; a plan derived from an approved roadmap item can auto-run |
| M-4 | Deciding what to work on next | No task selection exists | **Yes** — read `NEXT_TASK.md` from the target repository. That is what those files are for |
| M-5 | Reviewing and merging the integration branch | Deliberate. The line | **No, by design** |
| M-6 | Resolving an approval | Deliberate for sensitive classes | **Partly** — auto-approve low-risk classes by label, never schema or credential changes |
| M-7 | Noticing a run is stuck | No circuit breaker; a run can burn its budget failing the same way | **Yes** — N repeated failures pauses and escalates |
| M-8 | Cleaning up worktrees and transcripts | Q5, unbuilt | **Yes** — retention policy |
| M-9 | Triggering a run | Nothing is scheduled, by choice | **Yes** — a trigger API |
| M-10 | Carrying knowledge between runs | No memory beyond one run's event log | **Yes** — a project-memory store |
| M-11 | Judging whether output is correct beyond the test suite | Gates are the project's own checks | **Partly** — an embedder-supplied `GateRunner` extends what "correct" means, without the engine defining it |
| M-12 | Noticing the model backend degraded | Health checks at startup only | **Yes** — periodic health, and refusing to start work against an unhealthy provider |
| M-13 | Bounding spend across a run | Per-attempt budgets only | **Yes** — per-task and per-run ceilings |
| M-14 | Keeping documentation current | Convention, enforced by review | **Yes** — a docs gate: a task that changed code and not its living documents fails |

---

## 3. What must exist for each level

### L2 — Partial autonomy · one task away

| Need | Status |
|---|---|
| Decompose a goal into a DAG | ✅ `planner/` |
| Execute in dependency order under a cap | ✅ `task/`, `workflow/` |
| Isolate every attempt | ✅ `git_manager/` |
| Gate on the project's own tests | ✅ `test_runner/` |
| Retry with feedback | ✅ |
| Resume after interruption | ✅ |
| Observe live | ✅ SSE + WS |
| Assemble the above and run it | ✅ `3f59746` |
| **Isolate attempts, and gate what they produce** | ✅ **OP-002**, done — L2's exit criterion is met |

### L3 — Conditional autonomy · unattended for hours

| Need | Status | Task |
|---|---|---|
| A real backend, billed predictably | ✅ | done — `provider/claude_cli.py` (M18) |
| Budgets that bind when tokens are unmeasurable | ✅, then superseded | OP-004 shipped estimate-honest budgets; OP-014 went further and reports **measured** tokens from the CLI's own envelope. The token axis's *enforcement* on a one-shot harness is a separate, still-open question — OP-017, deliberately deferred |
| Per-task and per-run cost ceilings | ❌ | D.1 |
| Retention for worktrees and transcripts | ❌ | OP-009 (Q5) |
| Disk-pressure guard | ❌ | D.3 |
| Circuit breaker on repeated identical failure | ❌ | D.4 |
| Escalation as an approval rather than a stall | 🟡 queue exists; nothing raises one automatically | D.4 |
| Periodic provider health | ❌ | M-12 |

**The L3 hazard.** A system that runs for hours and fails silently is worse than
one that stops. Every item above is a *stopping* mechanism, not a speed one. Build
them before running unattended, not after the first overnight run burns a budget
producing nothing.

### L4 — High autonomy · continuous development

| Need | Status | Task |
|---|---|---|
| Triggers: schedule, webhook, run-completion | ❌ | E.1 |
| Run chaining with outcome conditions | ❌ | E.2 |
| Backpressure — one run per repository, triggers coalesce | ❌ | E.3 |
| **Task selection** — read the target's `NEXT_TASK.md` and turn it into a workflow | ❌ | new |
| **Project memory** across runs, with provenance and decay | ❌ | F.1–F.4 |
| A documentation gate | ❌ | M-14 |
| Reporting a human will actually read | 🟡 dashboard exists; no digest | new |

**Task selection is the piece that makes the living documents load-bearing.**
`NEXT_TASK.md` in each repository is not a note to a human — it is the input to
an autonomous planner. That is why both files are written as executable
specifications with scope, implementation notes, tests, and a definition of done.
A planner turning a roadmap line into a workflow should need nothing else.

---

## 4. The Hermes boundary

Hermes sits above this engine. Two systems, two jobs, and the split must stay
clean or both rot.

| Concern | Owner |
|---|---|
| What to work on next, across repositories | **Hermes** |
| Long-term direction and priority | **Hermes** |
| Which provider serves which role, per policy | **Hermes** (expressed as config) |
| Monitoring progress, restarting failed jobs | **Hermes**, through the API |
| Reviewing commits at a portfolio level | **Hermes** |
| Long-term memory across projects | **Hermes** |
| Decomposing one goal into a DAG | OrchestratorPro |
| Executing, isolating, gating, retrying, merging | OrchestratorPro |
| Budgets, history, approvals, recovery | OrchestratorPro |
| Memory **about one project** | OrchestratorPro (F.1) |

**Hermes talks to this engine over HTTP and nothing else.** No shared database,
no Python imports, no reaching into SQLite. The same rule the dashboard already
obeys, for the same reason: a second consumer that bypasses the API grows a
second idea of what a run is, and the first time they disagree nobody can say
which is right.

**What Hermes needs that does not exist yet:**

| # | Need | Task |
|---|---|---|
| H-1 | Trigger a run from outside on a schedule or an event | E.1 |
| H-2 | Ask "is this repository busy?" and get a real answer | E.3 |
| H-3 | A run digest — what changed, what it cost, what failed and why | new |
| H-4 | Subscribe to *all* runs, not one | ✅ `GET /events` |
| H-5 | Restart a failed job idempotently | ✅ `POST /runs/{id}/resume` |
| H-6 | Read and write project memory | F.1 |
| H-7 | Authenticate as a service with a scoped role | ✅ API keys, three roles |
| H-8 | Discover engine capability and version | ✅ `/health`, `/config` |

Five of eight already exist. The engine is closer to Hermes-ready than to
self-starting.

---

## 5. Safety invariants that never relax

Autonomy increases; these do not move. Each exists because removing it converts a
recoverable failure into an unrecoverable one.

1. **The human decides what merges.** No auto-merge to a default branch, at any
   level, ever.
2. **The agent never accepts its own work.** It reports; a gate decides. An agent
   that could mark itself green is an agent with no gate.
3. **`failed` ≠ `errored`.** Autonomy multiplies the cost of this confusion: an
   unattended run told "tests failed" when the harness is broken will spend its
   whole budget editing correct tests.
4. **Isolation is not optional.** One workspace per attempt. There is no fast
   path that shares a working tree.
5. **The agent is untrusted input.** Path confinement on the resolved path, an
   executable allowlist, `argv` never a shell string, destructive Git behind one
   audited chokepoint. More autonomy means more unreviewed tool calls, which
   makes these more load-bearing, not less.
6. **No silent degradation.** A capability not declared must raise, not no-op. An
   unattended run cannot notice that it silently got something cheaper than it
   asked for.
7. **Everything is attributable.** Every state change is an event; every approval
   carries the credential that made it. An autonomous system that cannot explain
   what it did is one nobody can be accountable for.
8. **Bounded spend, always.** No run without a ceiling. This is the one invariant
   the current build does not yet satisfy above the attempt level.

---

## 6. Honest assessment

**What is genuinely ready.** The hard, subtle parts are done and done well: an
authoritative event log with pure-fold replay, a deterministic scheduler,
worktree isolation via plumbing, the failed/errored distinction, capability
honesty, a trust boundary that is structural rather than advisory. These are the
things that are expensive to retrofit, and they are already right.

**What is missing is now entirely stopping conditions, not plumbing.** The
composition root and the CLI provider — the two hardest wiring gaps — are both
done (OP-002, M18). What is left is cost ceilings, retention, triggers, and a
circuit breaker. None of it is architecturally hard; all of it is necessary
before a human can walk away.

**The real risk is not the model.** It is the class of failure where the system
appears to work and does not: a gate that never ran reported as a pass, a
capability silently downgraded, an unbounded run that spends all night failing
the same way. Every such failure in this design is already named and refused
somewhere in the code. The work ahead is to keep refusing them as the human moves
further from the loop.

**The uncomfortable observation, and how it was closed.** Seventeen milestones
of careful, layer-respecting work had produced a system that had never been
assembled, and no test noticed, because every test assembled it itself. That
was precisely the failure mode autonomy amplifies: local correctness
everywhere, and no one asking whether the whole thing runs. `tests/e2e/
test_serve_execution.py` — the first end-to-end test written against the
*production* composition path — found it, and OP-002 closed it; eight real
runs against a real repository since then are the evidence it stayed closed.
The lesson generalizes past that one gap: a test that assembles its own
subject proves the subject can be assembled that way, not that it is.
