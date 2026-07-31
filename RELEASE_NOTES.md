# OrchestratorPro v1.0.0

**Released:** 2026-07-26 · **Tests:** 2674 passed, 3 skipped · **Python:** 3.11+

A self-hosted control plane for fleets of AI coding agents. You give it a goal
and a repository; it produces a task graph, runs each task as an isolated agent
in its own Git worktree, gates the result on the project's own tests, and merges
what passes. You supervise outcomes, not keystrokes.

Everything runs on your machine. The only outbound calls are to the model
backend you configure and whatever the repository's own tooling makes.

---

## Major features

### Orchestration

- **Declarative workflows.** A YAML file or an LLM-generated plan, both validated
  and compiled by the same code, so a generated plan is exactly as executable —
  and exactly as refusable — as a hand-written one.
- **A pure-function scheduler.** Graph plus state in, decision out. No I/O, no
  clock, no randomness, which is what makes "determinism of orchestration" a
  property with property tests rather than an aspiration.
- **Concurrency with dependency order**, global and per-label caps, and backoff
  timers that run outside the concurrency slot so a flaky run does not lose
  throughput to sleeping retries.
- **Retry with feedback.** A failed attempt's gate output is fed into the next
  attempt's prompt. Attempt #2 knows why #1 failed.
- **Resume.** A run killed at any instant replays from the event log without
  redoing completed work. An attempt cut off mid-flight is refunded rather than
  charged, so even a single-attempt step survives a crash.

### Agents

- **Every attempt is isolated** in its own `git worktree` on its own branch. The
  operator's checked-out tree is never touched, and no two attempts share a
  working tree.
- **Budgets bind on three axes** — wall clock, tokens, tool calls — whichever
  comes first. Exhaustion is a normal outcome with partial work preserved, not an
  exception.
- **Agent output is untrusted input.** Every model-supplied path is canonicalized
  and checked for containment on the *resolved* path; `run_command` uses an
  executable allowlist and rejects shell metacharacters.
- **Provider-independent.** Anthropic, any OpenAI-compatible endpoint (Ollama,
  vLLM, LM Studio, Open WebUI), and self-hosted Hermes. Backends declare their
  capabilities honestly rather than pretending to support what they do not.

### Gates and builds

- **The project's own tests decide.** pytest, JUnit XML, and generic exit-code
  parsers, with the failing case names and captured output carried into the
  retry.
- **Failed and errored are different verdicts** throughout: a gate that failed is
  a judgement about the code, a gate that errored is a judgement about the
  harness. They reach the UI as red and amber.
- **Incremental builds that are correct before they are fast.** A unit whose
  sources changed is rebuilt and so is everything downstream of it, transitively.
  Change is detected by content, never timestamps, and a cache hit is verified
  against the disk before it is honoured.

### Control plane

- **44 endpoints** — runs, tasks, workflows, agents, builds, approvals, auth —
  with SSE *and* WebSocket carrying identical event content.
- **Reads replay the event log** rather than reading the materialized tables:
  slower, and it cannot disagree with the record.
- **A dashboard with no build step.** Plain ES modules and CSS, no bundler, no
  package manager, nothing loaded from another origin. It works on a machine with
  no internet access.

### Operations

- **Authentication with a single-operator default.** Three ordered roles
  (viewer / operator / admin), HS256 tokens, scrypt passwords, hashed API keys,
  and an append-only audit log. With no account bootstrapped every route serves
  as an operator — and says so, loudly, on every start.
- **Human approval gates.** Hold a task for review; a reviewer reads the attempt
  history, the transcript of what the agent actually did, and the diff it
  produced, then approves, rejects, or sends it back.
- **Retention that archives before it prunes and verifies before it deletes.**
  Verification replays the archive to the same state rather than merely
  checksumming its bytes.
- **Hardening that fails closed.** A non-loopback bind is refused — not warned
  about — unless the deployment names its allowed hosts, enables rate limiting,
  and configures a token variable.
- **Docker, Compose, and installers** for Windows and Linux; online backups with
  verified manifests; versioned configuration migration.

---

## Architecture summary

```
Layer 5  dashboard/                            presentation
Layer 4  api/                                  HTTP + WS control plane
Layer 3  builder/   planner/   workflow/       builds, construction, execution
Layer 2  agent/     task/                      agent runtime, scheduler
Layer 1  adapter/   git_manager/  test_runner/ backends, VCS, gates
Layer 0  provider/  core/                      models, config, storage

         ops/    auth/                         cross-cutting
```

A module imports from its own layer and below, never above, and tests enforce the
parts that matter: nothing below the API layer imports FastAPI, Starlette,
Pydantic, or httpx, and only `provider/` names a vendor. The dashboard imports
nothing from `core`, `task`, `workflow`, or `builder` — it consumes the HTTP API
like any other client.

Three ideas carry most of the weight:

**The event log is authoritative.** Every state change is an append-only event;
the SQL tables are a materialized view maintained in the same transaction, and
`RunStore.verify()` compares the two. Recovery is replay, not guesswork — the
projection from events to state is a pure fold with no database, no clock, and no
filesystem. SQLite with WAL and `synchronous=FULL`, protected by append-only
triggers so the log's integrity is the engine's job rather than caller
discipline.

**Pluggable seams are `Protocol`s, not base classes.** `Provider`, `AgentPort`,
`GateRunner`. A build reaches the workflow engine by satisfying `GateRunner`,
which is why `builder` and `workflow` can be independent siblings that never
import each other.

**Prompt caching is a prefix match.** System prompts are frozen and checked for
volatile content at construction — a timestamp, a UUID, or a run id in a cached
prefix raises rather than quietly costing money. There is a test that renders a
prompt twice and compares hashes.

---

## Known limitations

Stated plainly, so nobody goes looking for something that is not there. **This
list is frozen at the v1.0.0 release (2026-07-26).** Two items below have since
been resolved by post-v1.0 work (M17, M18) — marked inline rather than edited
away, so this remains an accurate record of what shipped on this date. Current
status lives in `TECH_DEBT.md`.

- ~~**No end-to-end run against a real model backend has been performed.**~~
  **Resolved after release.** True on 2026-07-26; false since `f1ac955`
  (2026-07-27) — eight real runs have since executed against a real repository
  , several with real code adopted.
- **Worktrees and transcripts have no retention policy.** They accumulate. Runs,
  backups, and sessions do have one. *(Still true — Q5/OP-009.)*
- **The dashboard has no approval page.** The queue, transcripts, and diffs are
  reachable over HTTP only. This is the one place where the API is ahead of
  the UI. *(Still true.)*
- **The planner decomposes one level.** It does not recurse. *(Still true —
  and, separately, the planner has never been run on a real task; see OP-015.)*
- **One repository per run.** *(Still true.)*
- **No per-task cost limit**, only per-attempt budgets. *(Still true.)*
- ~~**`orchestrator/adapter/` is an empty package.**~~ **Resolved after
  release.** True on 2026-07-26; false since M18 — `adapter/base.py` declares
  `AgentPort`, and `adapter/claude_code.py` (the Claude Code CLI harness) is
  the second implementation the original text says is missing.
- **Nothing is scheduled.** Backups and retention are commands, not cron. The
  host already has a scheduler and this system does not want to be one.
  *(Still true.)*

---

## Future roadmap

Not commitments — the shape of the next work, in rough priority order.

1. **Retention for worktrees and transcripts.** The largest gap, and the one an
   operator hits first. *(Still open — Q5/OP-009.)*
2. ~~**A live run against a real backend**, promoted from a marked test to a
   documented worked example.~~ **Done** — not via a promoted test, but via
   eight real runs against a real desktop application.
3. **The approval page**, closing the one UI gap. *(Still open.)*
4. ~~**The second adapter.** The Claude Agent SDK harness behind the existing
   `Adapter` protocol, which is what would prove the seam is real.~~ **Done,
   differently.** The second implementation is `ClaudeCodeHarness`, driving
   the Claude Code **CLI**, not the Claude Agent SDK library — the seam is
   `AgentPort`. The Agent SDK itself remains unintegrated and unplanned.
5. **Per-task and per-run cost ceilings**, above the per-attempt budgets.
   *(Still open — D.1 in `ROADMAP.md`.)*
6. **Recursive planning**, if one level turns out to be the wrong answer. It was
   a deliberate choice, and the evidence for changing it would be steps that are
   consistently too large.
7. **Splitting `core/events.py`** into `core/ids.py`, `core/errors.py`, and
   `core/budget.py` — a pure move, outstanding since M1.

---

## Upgrading

There is nothing to upgrade from. Configuration carries a `config_version` and
`orchestratorpro config migrate` exists so that the next release has a path.

## Verifying this release

```bash
pytest -q                                    # 2674 passed, 3 skipped
python -m build                              # wheel + sdist
docker build -t orchestratorpro:1.0.0 .
orchestratorpro version
orchestratorpro serve --dry-run
```

Full guides are in `docs/`: installation (`100`), API (`110`), user manual
(`120`), developer guide (`130`).
