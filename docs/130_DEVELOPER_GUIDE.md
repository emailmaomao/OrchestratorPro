# Developer guide

For whoever works on OrchestratorPro next. `CLAUDE.md` is the working
agreement — the rules. This explains why they are the rules.

---

## Getting set up

```bash
git clone https://github.com/emailmaomao/OrchestratorPro.git
cd OrchestratorPro
./scripts/install.sh --dev          # or scripts\install.ps1 -Dev
.venv/bin/python -m pytest -q
```

The whole suite runs offline. Nothing in it reaches a network, launches a model,
or needs a container. Tests that *could* need those are marked and skip.

```bash
pytest -q                     # everything fast
pytest -q -m "not slow"       # skip the load and Docker tests
pytest -q -m live             # only the tests that need a real endpoint
```

Node is optional. If it is installed, `pytest` also runs the dashboard's own
unit tests through `node --test`; if not, those skip.

---

## The layering, and why it is enforced

```
L5  dashboard/                                  presentation
L4  api/                                        HTTP control plane
L3  builder/   planner/   workflow/             build, construct, execute
L2  agent/     task/                            agent runtime, scheduler
L1  adapter/   git_manager/   test_runner/      backends, VCS, gates
L0  provider/  core/                            models, config, storage
    ops/                                        hardening, backup, retention
    auth/                                       identity
```

A module imports from its own layer and below. Never above. Two import-layering
tests enforce the parts that matter most:

- **Nothing below L4 imports FastAPI, Starlette, Pydantic, or httpx.** The
  scheduler, the agent runtime, and the builder are meant to be usable from a
  script with nothing installed. One convenient `from pydantic import` in a
  domain module ends that quietly.
- **Only `provider/` names a vendor.** Everything above it speaks the neutral
  `ModelPort`.

Two same-layer pairs deliberately do not know about each other:

- `agent` and `task`. An agent receives a `TaskSpec` — a narrow value object —
  not a `Task`. The workflow engine translates. This keeps the agent runtime
  testable without a graph and the scheduler testable without an agent.
- `workflow` and `builder`. A build reaches the engine by satisfying
  `GateRunner`, not by being imported.

The dashboard is the strictest case: it consumes the HTTP API and imports
nothing from `core`, `task`, `workflow`, or `builder`. A UI that reaches past
the API grows a second idea of what a run is, and the first time the two
disagree nobody can say which is right.

---

## The ideas the codebase is built on

### The event log is authoritative

Every state change is an append-only event. The SQL tables are a materialized
view maintained in the same transaction, and `RunStore.verify()` compares the
two. If they ever disagree, the log wins and the tables are rebuilt from it.

Recovery is replay, not guesswork. `core/projection.py` is a pure fold from
events to `RunState` — no database, no clock, no filesystem — which is why it
can be tested exhaustively and why a `kill -9` at any instant is survivable.

Two asymmetries in that fold, both deliberate: an unknown event type is
*ignored*, so a log written by a newer build still replays on an older one; a
structurally impossible event *raises*, because that means damage and continuing
would produce a confidently wrong answer.

### The scheduler is a pure function

`task/scheduler.py` takes a graph and a state and returns a decision. No I/O, no
clock, no randomness. Everything that actually happens — starting attempts,
waiting, timing out — lives in `task/dispatcher.py`.

That split is what makes the ordering testable with property tests over randomly
generated DAGs, and it is why "determinism of orchestration" is a claim rather
than a hope.

### Failure and brokenness are different

A gate that *failed* is a verdict about the code. A gate that *errored* is a
verdict about the harness. The same distinction runs through builds
(`BuildStatus.FAILED` vs `ERRORED`) and reaches the UI as red vs amber.

It matters because feedback that says "tests failed" when the test runner is
broken sends the next attempt to edit tests that were fine. Anywhere you are
tempted to collapse the two, don't.

### Agent output is untrusted input

Every model-supplied path is canonicalized and checked for containment before
any I/O — on the *resolved* path, so `..`, a symlink pointing out, and an
absolute path elsewhere are all rejected. `run_command` uses an executable
allowlist, never a blocklist. The dashboard builds no markup from strings.

This extends further than it first appears: a workflow YAML file may have been
written by an agent, so it is parsed with `yaml.safe_load` and its paths are
validated like any other.

### Prompt caching is a prefix match

Any byte change invalidates everything downstream. So system prompts are frozen
and checked for volatile content at construction — a timestamp, a UUID, or a run
identifier in a cached prefix raises rather than quietly costing money. Retry
feedback goes in the message, never the prefix.

There is a test that renders a prompt twice and compares hashes.

---

## Working on it

### Milestones

`TASKS.md` is the plan and the record. Work one milestone at a time, in order.
Update it in the same commit that completes one, and be honest in it — the
scope notes explaining what was *not* built have been more useful than the
checklists.

If the spec and the code disagree, the spec wins. If the spec is wrong, amend it
in the same commit. There are several places where a milestone redefined
something; each says so.

### Tests

Colocated under `tests/`, mirroring the package tree.

- **No live API calls in the default suite.** Recorded fixtures and scripted
  transports. Live tests are marked and never required.
- **`git_manager` tests run against real temporary repositories.** Mocking Git
  would test nothing — the failure modes are index state and file locks.
- **The scheduler gets property tests** over randomly generated DAGs.
- A test that would pass if the feature were deleted is not a test.

Write the assertion that would have caught the bug, not the one that describes
the code. Several bugs in this repository were found by tests written that way:
a token bucket that treated a monotonic zero as "never used" and limited
nothing; an append-only trigger silently dropped by a DDL splitter; a
correlation header looked up with a bytes key against a string-keyed dict.

### Style

`mypy --strict -p orchestrator` is the target and is **not currently met** —
46 errors in 14 files, tracked as OP-008 with per-line detail in
`TECH_DEBT.md`. New code should not add to that count; a change that
introduces a new mypy error under `--strict` is a review reject even though
the baseline itself is not yet clean. Frozen dataclasses for domain objects;
Pydantic only at the HTTP boundary. `Protocol` for pluggable seams, no ABCs,
no inheritance deeper than one level. Every custom exception derives from
`OrchestratorError` and declares a stable `code` and `retryable`. No `print`.

Comments state constraints the code cannot express — not what the next line
does. If a decision was non-obvious, the comment explains the alternative that
was rejected and why.

### Async

`asyncio` throughout. A blocking call on the event loop is a bug: subprocesses,
Git, and file I/O over a few kilobytes go through `asyncio.to_thread` or an
async library.

---

## Where things live

| Package | Owns | Deliberately does not own |
|---|---|---|
| `core` | Config, logging, errors, ids, storage, the event log | Any domain concept |
| `provider` | Model calls, token accounting, refusals, caching | Tasks, files, Git |
| `adapter` | The agent-harness seam (`AgentPort`): our own tool loop and the Claude Code CLI harness | Deciding whether an attempt's output is acceptable — that is the gate's job |
| `agent` | Prompt assembly, budget enforcement, the tool loop | The DAG |
| `task` | Domain model, state machine, scheduler | I/O of any kind |
| `git_manager` | Worktrees, branches, merges, the destructive-op guard | When to create one |
| `test_runner` | Gate execution and parsing | What to do with a verdict |
| `workflow` | The execution loop, retries, resume, approvals | How an agent works |
| `builder` | Project analysis, incremental plans, the build cache | When a build is wanted |
| `planner` | YAML and LLM plans → validated workflows | Executing them |
| `auth` | Identity, roles, sessions, the audit trail | What any of it may do |
| `ops` | Hardening, rate limits, backup, retention, benchmarks | Business logic |
| `api` | HTTP/WS surface, serialization | Business logic |
| `dashboard` | Presentation | Anything else |

---

## The frontend

Plain ES modules and CSS. No build step, no bundler, no package manager, and
nothing loaded from another origin — the UI works on a machine with no internet
access, and it cannot rot independently of the backend.

Two invariants, both tested:

- **One module talks to the network.** Facts enter through `api.js`; every other
  module receives data and returns nodes.
- **No markup is built from a string.** Goals, prompts, and diagnostics are
  agent output. The UI writes them as text.

Pure logic — the API client, the formatters, the graph layout — lives in modules
with no DOM access and has real unit tests under `tests/dashboard/js/`. The rest
is covered structurally: imports resolve, no CDN, no `eval`, every route points
at a module that exists.

---

## Adding things

**A provider.** Implement `ModelPort` in `orchestrator/provider/`, register a
factory. Declare capabilities honestly — a backend that cannot do prompt caching
should say so rather than quietly ignoring the hint. The conformance
expectations are in `docs/030_PROVIDER_INTERFACE.md`.

**A gate.** Implement `GateRunner`: `run(cwd, spec) -> Verdict`. That is the
whole contract; `BuildGate` is an example that is not a test runner.

**An endpoint.** Add it to `api/routes.py` with a summary and a tag, declare the
least role that may reach it, and add a response model. A route with no summary
is one nobody can find in the schema, and a test enforces that too.

**A configuration key.** Add it to the dataclass in `core/config.py`. If you
*rename* one, add a migration step in `ops/migrate.py` and bump `CONFIG_VERSION`
— validation is strict, so a rename breaks every existing installation on
upgrade, loudly.

---

## Releasing

```bash
pytest -q                                 # everything green
pytest -q -m slow                         # load and Docker
ruff check orchestrator/ --select S       # flake8-bandit; a finding blocks
pip_audit -r requirements.txt             # known CVEs in pinned deps
python -m build --wheel                   # a wheel that carries the assets
docker build -t orchestratorpro:x.y.z .
```

The security lane is not optional and is not CI-enforced — there is no CI in
this repository. A finding gets fixed, or waived on its own line with a
reason in `SECURITY.md`; `tests/ops/test_security.py` fails if a waiver has
no reason, if `S` leaves the ruff selection, or if `assert` reappears in
`orchestrator/`.

Bump the version in `pyproject.toml` and `orchestrator/cli.py` together; a test
checks they agree.

---

## Still open

Recorded rather than forgotten:

- **Retention for worktrees and transcripts.** Backups and runs have policies;
  these do not.
- **Recursive planning.** The planner decomposes one level.
- ~~`orchestrator/adapter/` is an empty package.~~ **No longer true.**
  `adapter/base.py` declares `AgentPort` (`docs/030` §5.2's protocol, written
  once a second harness existed to write it from); `adapter/claude_code.py` is
  that second harness, driving the Claude Code CLI as an external process. Two
  real implementations satisfy the seam: `agent/runtime.py::AgentRuntime` (the
  tool loop) and `ClaudeCodeHarness`.
- **`core/events.py` still holds the consolidated primitives** that were meant
  to be split into `core/ids.py`, `core/errors.py`, and `core/budget.py`.
- **`mypy --strict -p orchestrator`: 46 errors in 14 files (OP-008).** See
  "Style" above and `TECH_DEBT.md` for the per-line breakdown.
