# User manual

For the person running OrchestratorPro day to day: how to describe work, watch
it happen, and decide what to do when it goes wrong.

---

## The shape of the thing

You give OrchestratorPro a **goal** against a **repository**. It becomes a
**task graph**. Each task is handed to an **agent** working alone in its own Git
worktree, bounded by a **budget**. When the agent says it is done, the project's
own tests decide whether that is true. Work that passes is merged; work that
fails is retried with the failure fed back into the next attempt.

You supervise outcomes, not keystrokes.

Two facts worth holding on to, because most of the surprises follow from them:

- **An agent sees its own prompt and the repository. Nothing else.** Not the
  goal, not the other tasks, not what they produced. Every prompt has to stand
  alone.
- **The event log is the truth.** Every screen is a view of it. A run can be
  killed at any instant and reconstructed exactly.

---

## Describing work

### A YAML workflow

```yaml
name: migrate-http-client
goal: replace requests with httpx across the client
max_concurrency: 3

defaults:
  max_attempts: 2
  gates: [tests]

steps:
  - name: client-uses-httpx
    prompt: |
      Replace the requests-based client in src/client.py with httpx.
      Keep the public function signatures unchanged.
      Done when the module imports httpx and no longer imports requests.

  - name: call-sites-updated
    prompt: |
      Update every call site to the new client's signature.
      Search for imports of src/client.py and adjust each one.
    depends_on: [client-uses-httpx]

  - name: docs-updated
    prompt: Update README.md and docs/ to mention httpx instead of requests.
    depends_on: [client-uses-httpx]
    gates: []

  - name: release-note
    prompt: Add a release note describing the migration.
    depends_on: [call-sites-updated, docs-updated]
    when:
      all_of:
        - did_work: call-sites-updated
        - did_work: docs-updated
```

Check it before running it:

```bash
orchestratorpro workflow check migrate.yaml
```

Every problem is reported at once, with a path — `steps[2].depends_on[0]` —
rather than one at a time.

### Writing a good prompt

The prompt is the entire brief. Write it for a competent engineer who has the
repository open and no other context.

- Say what to change, where, and what "done" means.
- Never write "as discussed" or refer to another step.
- Prefer few substantial steps over many trivial ones. Each step costs a
  checkout, an agent, and a gate run.
- Declare a dependency only when the later step genuinely needs the earlier
  one's output. A dependency that is not real costs parallelism for nothing.

### Conditions

`when` prunes a branch without failing it:

| Condition | True when |
|---|---|
| `did_work: X` | X ran and succeeded — as opposed to being skipped |
| `was_skipped: X` | X was pruned |
| `all_of`, `any_of`, `not` | Combinators |

A condition may only reference a step this one depends on. Otherwise it could
be evaluated before that step had finished, and the answer would depend on
timing.

### Or let a model plan it — not yet reachable this way

**Corrected.** The command above registers a hand-written or already-generated
workflow *definition* (`POST /workflows`); it does not invoke the planner.
Neither the API nor the CLI currently exposes a path that takes a goal and
returns a plan — `orchestrator/planner/llm.py`'s `WorkflowPlanner` exists,
is unit-tested, and is proven equivalent to hand-written YAML
(`tests/planner/test_integration.py` builds a graph both ways and asserts they
match node for node), but nothing in `cli.py` or `api/routes.py` wires it up
for a caller to reach. **It has also never been run against a real task** —
every exercise of it to date is against fakes or fixtures, not a real
repository (`OP-015`, still open).

Today, "let a model plan it" means: call `WorkflowPlanner` yourself as a
library from Python, review what it returns, then register the resulting
definition the normal way — the same
`POST /workflows` shown above, once you have a plan in hand by whatever means.
A generated plan is a proposal regardless of how it reaches this API. Read it
before running it.

---

## Watching a run

Open <http://127.0.0.1:8765/ui/>.

**Runs** lists everything, with a live status. **Open** a run for its task
graph, its task table, and its log — all updating from a single shared event
stream rather than by polling.

The graph is colour-coded, and the colours mean specific things:

| | |
|---|---|
| grey | pending — nothing has started |
| blue | running or gating |
| green | succeeded |
| red | failed or abandoned |
| amber | retrying, or a build tool that broke |

That last one matters. **Amber is not "nearly red".** A build or gate that
*errored* verified nothing — the tool broke. Red means the code was checked and
found wanting. Telling an agent "tests failed" when the test runner is broken
sends it off to edit tests that were fine.

### Status, precisely

- **complete** — every step reached a terminal state.
- **healthy** — nothing failed or was blocked.

A run can be complete and unhealthy. The dashboard shows "failed" for that, not
"succeeded".

---

## When something goes wrong

### A step failed

Open the run, find the red node, read its row. The log shows the gate verdict
and the failing test names — "tests failed" alone is not actionable, so the
system does not stop there.

If it has attempts left it will retry by itself, and the next attempt is told
what the previous one did wrong.

### It failed the same way three times

Look at **attempt history** for that task: `repeated_failures` names the gates
that failed more than once. The same gate failing every time usually means the
prompt is wrong, not the code — the agent is doing what you asked and what you
asked is not what you wanted.

Amend the prompt and start a new run. A task that has already started cannot be
amended; its prompt is part of the record by then.

### The run stopped early

Cancel is not failure. A cancelled run is *stopped*, not finished, and can be
resumed:

```bash
curl -X POST 'localhost:8765/runs/run_01J.../resume?workflow=migrate-http-client'
```

Resuming does not redo completed work. An attempt that was cut off mid-flight is
not charged against the task's allowance — it produced no outcome — so a
single-attempt step is still resumable after a crash.

A step that failed on its own terms is *not* retried by a resume. Pass
`retry_failed=true` if that is what you want.

### The whole thing is stuck

`GET /runs/{id}/status`. If `running` is zero and `pending` is not, something is
blocked behind a failed dependency. The task table shows which.

---

## Approvals

Some work should not merge because a test passed. A migration, a credential
rotation, anything whose failure mode shows up in production.

Request review on a task, and the queue holds it:

```bash
curl -X POST localhost:8765/runs/$RUN/tasks/$TASK/approval \
  -H 'content-type: application/json' -d '{"reason":"touches the schema"}'
```

A reviewer sees the attempt history, the transcript of what the agent actually
did, and the diff it produced. Then:

- **Approve** — the work proceeds.
- **Reject** — the task is abandoned. It is not deleted; the record stays.
- **Retry** — the task goes back for another attempt.

The decision is recorded against whoever made it, from their credential. The
queue is oldest-first.

---

## Builds

If your project has a build, OrchestratorPro can run it incrementally and use it
as a gate.

```bash
curl -s localhost:8765/builds/plan -H 'content-type: application/json' \
  -d '{"path":"/work/repo","changed_paths":["src/core/util.py"]}'
```

The plan says what would be rebuilt **and why**. The rule is short: a unit whose
sources changed is rebuilt, and so is every unit downstream of it, transitively.
That second half is what makes an incremental build correct rather than fast.

Change is detected by content, never by timestamps — a checkout, a clock skew,
or a file copied back into place does not trigger a rebuild. A cache hit is
checked against the disk before it is honoured; if the artifacts are gone, the
unit is rebuilt.

---

## Costs

Every token is attributed to a task attempt. **Metrics** shows totals; a run's
detail shows its own.

Budgets bind per attempt on three axes — wall clock, tokens, tool calls —
and whichever binds first ends the attempt. They fail differently: a task can
spin cheaply for an hour, burn a fortune in one turn, or thrash on tool calls
without spending much of either.

A model that reports no cost shows as "not reported", never `$0.00`. An unpriced
run is not a free one.

**Tokens may be measured or estimated, and the run tells you which.** A run's
`usage.tokens_estimated` (and each attempt's own copy of the flag) is `false`
when a backend reported real usage and `true` when the number is an
approximation — roughly four characters per token — because the backend
declared no token-counting capability. One estimated attempt makes the whole
run's total an estimate; the dashboard prefixes estimated totals with `~`. The
subscription-billed Claude Code CLI backend (M18) reports **measured** usage
from its own envelope — `tokens_estimated: false` — but never a dollar figure:
`cost_usd` stays `null`, alongside a separate **`notional_cost_usd`** — a
scale estimate against public pricing, charged to nobody, useful for judging
whether a run was cheap or expensive, not for an invoice.

---

## Housekeeping

```bash
orchestratorpro backup create ./backups --label weekly
orchestratorpro retention plan
orchestratorpro retention apply ./archives --yes
```

Archived runs are removed from the live database but not lost; they are verified
before removal and can be restored. Backups are snapshots of the whole database
and are verified against a digest.

Neither is optional if the run history matters to you. Nothing here is
automatic — the host already has a scheduler and this system does not want to
be one.

---

## What this build does not do

Stated plainly so you do not go looking:

- **Worktrees and transcripts have no retention policy.** They accumulate.
  Backups and archived runs do have one.
- **There is no per-task cost limit**, only per-attempt budgets.
- **The planner is one level.** It does not decompose recursively.
- **One repository per run.**
- **The LLM planner has no reachable path today.** It exists and is tested,
  but neither the API nor the CLI exposes a way to call it — see "Or let a
  model plan it" above. It has also never been run against a real task.
- **No gate registry.** `[gates]` reaches the built-in test runner only; an
  embedder cannot register a domain-specific `GateRunner` yet.
- **No artifact manifest.** A non-Git task's output is reachable only by
  scraping a workspace or a transcript, not through a dedicated endpoint.
