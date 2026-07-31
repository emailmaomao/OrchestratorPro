# 000 — Project Vision

**Status:** Approved for M0. **Vision document — not restated as milestones
land**; see `ORCHESTRATOR_PRO_SPEC.md` §12 and `TASKS.md` for what is actually
built (M0–M18, complete).
**Last updated:** 2026-07-31 (status note added; body unchanged since M0)

---

## 1. The problem

A capable coding agent can hold one task in its head and do it well. The
bottleneck is no longer the quality of a single agent turn — it is everything
around it:

- **Serialization.** A twelve-file refactor is twelve independent edits, but a
  single agent session does them one at a time, in one context, degrading as the
  context fills.
- **Contamination.** Parallel agents in one working tree overwrite each other.
  The obvious fix — run them serially — throws away the parallelism that made
  fan-out attractive.
- **Unverified output.** An agent that says "done" is making a claim. Without a
  gate, the operator either trusts it or reviews everything by hand, and at fleet
  scale hand-review is the new bottleneck.
- **No memory of failure.** When an attempt fails, the next attempt usually
  starts from zero rather than from *why the last one failed*.
- **Opacity.** Long autonomous runs are a black box. Cost, progress, and blockage
  are invisible until the run ends.

Each of these is an orchestration problem, not a model problem. Better models
make the individual turns better; they do not make the fleet coherent.

## 2. What OrchestratorPro is

A control plane that supplies the missing structure:

- A **task graph** so work is decomposed and parallelism is explicit.
- A **worktree per attempt** so agents cannot contaminate each other or the
  operator's checked-out tree.
- A **test gate** so "done" means "the suite is green", not "the agent said so".
- **Retry with feedback** so a failed attempt teaches the next one.
- An **append-only event log**, an API, and a dashboard so a run is observable
  while it happens and auditable afterwards.

The operator's job becomes: state the goal, review the plan, review the diff.

## 3. Who it is for

**Primary: the individual engineer with a large mechanical change.** Framework
migrations, dependency upgrades across many call sites, adding a cross-cutting
concern, test backfill. Work that is well-specified, wide, and tedious — the
shape where fanning out pays and where a test suite already exists to judge the
result.

**Secondary: the small team running background maintenance.** Nightly
dependency bumps, lint-debt paydown, flake triage. Runs that nobody watches and
that therefore need audit trails and hard gates more than they need interactivity.

**Explicitly not for: greenfield product design.** When the destination is
unclear, decomposition into a static DAG is premature and the tool fights you.
Use a conversational agent until the shape is known, then bring the mechanical
part here.

## 4. Principles

**The human decides what merges.** OrchestratorPro produces an integration
branch and stops. It never merges to `main`. Automating the last step would trade
the operator's judgment for throughput, which is the wrong trade at any speed.

**Verification beats trust.** Every claim of completion passes through a gate the
project already owns. We consume the test suite; we do not invent a new notion of
correctness.

**Isolation is not optional.** One worktree per attempt, always. There is no
"fast path" that shares a working tree, because that path is where correctness
goes to die.

**Orchestration is deterministic even though models are not.** The scheduler is a
pure function. Given the same graph and the same agent outputs, it makes the same
decisions. Non-determinism is confined to the model boundary and logged there.

**Failure is data.** A failed attempt keeps its worktree, its transcript, and its
gate output. The next attempt is told what went wrong. Nothing is thrown away
just because it did not work.

**Local-first, no lock-in.** Runs on the operator's machine. State is SQLite and
JSONL on disk. Outputs are ordinary Git branches. If the tool disappears
tomorrow, the work is still there in a form Git understands.

**The agent is untrusted input.** Every path is confined, every command is
allowlisted, every destructive Git operation passes one audited chokepoint. Not
because agents are malicious, but because a harness that assumes good output is a
harness that breaks on the first bad one.

**No silent degradation.** Truncated context, dropped content, and swallowed
errors are bugs. If something cannot be done, the system says so.

## 5. What success looks like

Twelve months in, the tool is working if:

1. An operator can take a well-specified mechanical change across 20+ files,
   state it once, and review one integration branch — instead of driving 20
   agent sessions by hand.
2. A run that is killed mid-flight resumes without redoing completed work.
3. Every merged change was gated by the project's own tests. Zero exceptions.
4. Cost per run is known to the token, attributed per task, before the bill
   arrives.
5. When a run fails, the operator can tell *which* task failed and *why* from the
   dashboard, without reading raw logs.
6. Nothing merged to `main` without a human looking at it.

Counter-metric — the thing that would mean we built the wrong tool: operators
routinely turn gates off to make runs pass. If that happens, the gates are in the
wrong place or the decomposition is too coarse, and the fix is upstream, not a
softer gate.

## 6. Non-goals

| Not doing | Why |
|---|---|
| Hosted SaaS | Local-first is a design constraint, not a stage. Hosting changes the security model entirely. |
| Our own model | The provider layer exists so this stays someone else's problem. |
| Replacing CI | We consume the project's tests. Defining correctness is the project's job. |
| Auto-merge to `main` | See Principles. This is the line. |
| Generic workflow engine | The domain is software change on a Git repository. Generality here buys nothing and costs clarity. |
| IDE integration | A different product with different constraints. Possibly later; not a v1 compromise. |
| Multi-repository runs | One repo per run, resolved as a permanent answer, not a stepping stone — spec §13 Q3 is closed, not just not-yet-solid. |

## 7. Horizons

All three horizons below have shipped (M0–M18). Kept as written — the
ordering claim ("usability before durability, and both after correctness") is
what mattered, and it held.

**Now (M0–M7) — make it work.** ✅ Spec, core, provider, task graph, Git
isolation, gates, agent runtime, workflow engine. Ends with: a multi-task run
executes correctly, in parallel, gated, resumable, driven from Python.

**Next (M8–M10) — make it usable.** ✅ Declarative YAML, an LLM planner, the
API, the dashboard. Ends with: an operator who has not read the source can
state a goal and watch it run.

**Later (M11+) — make it durable.** ✅ Packaging, CLI, retention policies
(partial — worktrees and transcripts still lack one, Q5), performance.
Reconsidering the two non-goals above has not happened; neither IDE
integration nor multi-repo has resurfaced as a real need.

The ordering is deliberate: usability before durability, and both after
correctness. A tool that is pleasant and unreliable is worse than one that is
awkward and trustworthy, because the second one you can fix.
