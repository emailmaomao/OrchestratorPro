# OrchestratorPro

A self-hosted control plane for fleets of AI coding agents.

Give it a goal and a Git repository. It decomposes the goal into a dependency
graph of tasks, runs each task with an agent in its own isolated Git worktree,
gates every result on your project's own test suite, and merges what passes onto
an integration branch for your review.

You supervise outcomes, not keystrokes.

---

## Status

**v1.0.0, with two workstreams past it (M17, M18) also complete.**

| | |
|---|---|
| Version | **1.0.0** |
| Milestones | M0–M18 complete; what comes next is in [`ROADMAP.md`](ROADMAP.md) |
| Source | 78 modules, ~30,000 lines under `orchestrator/` |
| Tests | 2,412 test functions across 85 modules |
| API | 44 paths / 54 operations, plus a WebSocket stream |

> **`orchestratorpro serve` runs the full pipeline** when started with `--repo`:
> a `git worktree` per attempt, the project's own `[gates]` suite as a real
> gate, commits, and a serialized merge to `orchestrator/<run>/integration` —
> nothing pushes, and the operator's own checkout is never touched. Verified
> against a real project, not only against fakes: eight
> runs have executed this path end to end, several with real code adopted.
> Without `--repo` the server still runs, in a `fallback` mode that clamps
> concurrency to 1 and verifies nothing — see
> [`TECH_DEBT.md`](TECH_DEBT.md) §2 and [`ROADMAP.md`](ROADMAP.md)
> for what is still genuinely open (retention, the mypy baseline, a planner run
> against a real task).

Milestone history is in [`TASKS.md`](TASKS.md); current state, verified against
the source, is in [`TECH_DEBT.md`](TECH_DEBT.md).

---

## Quick start

### 1. Install

```bash
git clone https://github.com/emailmaomao/OrchestratorPro.git
cd OrchestratorPro

./scripts/install.sh        # Linux / macOS   (--dev adds test and lint tooling)
.\scripts\install.ps1       # Windows         (-Dev)

.venv/bin/orchestratorpro version
```

Docker Compose and `pip install -e .` work too — see
[`docs/100_INSTALLATION.md`](docs/100_INSTALLATION.md).

### 2. Point it at a model

Four backends are supported. The Claude Code CLI needs **no API key anywhere** —
it drives your existing subscription session as an external harness:

```bash
export ORCHESTRATORPRO__AGENT__ADAPTER=claude_code
export ORCHESTRATORPRO__AGENT__MODEL=sonnet
export ORCHESTRATORPRO__PROVIDER__CLAUDE_CLI__EXECUTABLE=claude

orchestratorpro config check      # exit 0 = valid, 3 = the deployment is unsafe
```

Credentials are never configuration: any config key that looks like a secret is
rejected at load time. Put the *name* of the variable in config, the value in
the environment.

### 3. Describe the work

A workflow is a goal and a few steps with dependencies between them. Steps that
depend on each other are cut from the integration branch in order, so a later
step sees an earlier step's committed work:

```yaml
name: extract-tag-service
goal: >
  Move tag filtering out of the UI layer into a service that can be
  unit-tested without constructing a widget.
max_concurrency: 1

defaults:
  max_attempts: 2
  gates: [tests]
  expects_changes: true

steps:
  - name: extract
    title: services/tag_filter.py — the logic, without the widget
    prompt: |
      Create `src/services/tag_filter.py` holding the filtering logic
      currently inside `src/ui/tag_panel.py`. It must import nothing from
      the UI framework. Behaviour must be identical.

      Add `tests/test_tag_filter.py` covering the empty-selection case and
      the multi-tag intersection case.

      Do NOT change the public API of TagPanel.

  - name: wire
    title: Route every caller through the new service
    depends_on: [extract]
    prompt: |
      Update every caller in `src/` to use the new service, and verify no
      import of the old code path remains anywhere.
```

Validate it before spending anything — an unknown dependency, a duplicate step
name, or a cycle is an error here rather than a deadlock later:

```bash
orchestratorpro workflow check workflows/extract-tag-service.yaml
```

### 4. Run it

> **`--repo` is a global option, so it goes _before_ the subcommand.**

```bash
orchestratorpro --repo /path/to/your-project run workflows/extract-tag-service.yaml
```

```
extract-tag-service: succeeded
  run     run_01J...
  steps   2/2 succeeded
  branch  orchestrator/run_01J.../integration
  review the diff before merging; nothing reached main
```

Exit codes are the contract: **`0`** every step succeeded, **`1`** something did
not, **`2`** the invocation was wrong, **`3`** it refused as unsafe. A scheduler
or CI job can branch on that without parsing anything.

Without `--repo` there are no worktrees, no gates, and no commits, so `run`
refuses rather than reporting success for unverified work. Pass
`--allow-unverified` if a dry run is what you actually want.

### 5. Review, then adopt it yourself

```bash
cd /path/to/your-project
git diff main...orchestrator/<run-id>/integration
git merge --no-ff orchestrator/<run-id>/integration
```

The engine never merges to `main`. That refusal is deliberate and there is no
flag to disable it.

### Or run the server instead

```bash
orchestratorpro --repo /path/to/your-project serve
```

Dashboard on <http://127.0.0.1:8765/ui/>, REST + SSE + WebSocket API alongside
it. Same pipeline; use it when you want live progress, the approval queue, or
another system driving runs over HTTP. The bind is loopback-only and refuses a
public address without allowed hosts, a rate limit, and a token variable.

---

## What it needs from your project

| | |
|---|---|
| **Git** | A repository with a clean tree. Isolation is the whole safety model. |
| **A test suite** | The gate *is* your tests. Configure `[gates] test_command`; the default is `pytest -q`. Without one, nothing verifies the agent's work. |
| **Test-shaped work** | Ask: if the agent got this wrong, would a test notice? Refactors, extractions, parsers, data transforms — yes. Anything visual — no; there is no GUI verification, and a run can pass every gate and still look broken. |

---

## Documents

Read in this order:

| Document | What it covers |
|---|---|
| [`TECH_DEBT.md`](TECH_DEBT.md) | **Start here.** Known gaps, debt, and where docs and code disagree. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | What is actually assembled, and the seams to extend it through. |
| [`API_STATUS.md`](API_STATUS.md) | The stable surface other systems build on, with its gaps. |
| [`ROADMAP.md`](ROADMAP.md) | What comes after v1.0. |
| [`AUTONOMY_ROADMAP.md`](AUTONOMY_ROADMAP.md) | How far this engine can run without a human, and what stands in the way. |
| [`docs/000_PROJECT_VISION.md`](docs/000_PROJECT_VISION.md) | Why this exists, who it is for, what it refuses to be. |
| [`docs/010_REQUIREMENTS.md`](docs/010_REQUIREMENTS.md) | Numbered functional and non-functional requirements. |
| [`docs/020_ARCHITECTURE.md`](docs/020_ARCHITECTURE.md) | Layering, module contracts, data flow, concurrency. |
| [`ORCHESTRATOR_PRO_SPEC.md`](ORCHESTRATOR_PRO_SPEC.md) | The authoritative technical specification. |
| [`TASKS.md`](TASKS.md) | Milestone plan and current status. |
| [`CLAUDE.md`](CLAUDE.md) | Working agreement for AI assistants in this repo. |

These documents are the source of truth. Where a document and the code disagree,
the document wins until it is amended.

---

## How it works

```
  goal + repo
      │
      ▼
  ┌────────┐   plan     ┌────────────┐  schedule  ┌───────────┐
  │ builder│ ─────────▶ │ task graph │ ─────────▶ │ workflow  │
  └────────┘            └────────────┘            └─────┬─────┘
                                                        │ dispatch
                              ┌─────────────────────────┼─────────────────┐
                              ▼                         ▼                 ▼
                        ┌──────────┐             ┌──────────┐      ┌──────────┐
                        │  agent   │             │  agent   │      │  agent   │
                        │ worktree │             │ worktree │      │ worktree │
                        └────┬─────┘             └────┬─────┘      └────┬─────┘
                             │ tests pass?            │                 │
                             └────────────────────────┴─────────────────┘
                                                 │
                                                 ▼
                                    integration branch, for your review
```

Every task attempt gets its own `git worktree` on its own branch. Attempts never
touch your checked-out working tree, and the system never merges to `main` —
that decision stays with you.

---

## Package layout

```
orchestrator/
  provider/      neutral ModelPort + four backends: Anthropic API, an
                 OpenAI-compatible endpoint (Ollama/vLLM/LM Studio), Hermes,
                 and the Claude Code CLI (subscription-billed, no API key)
  adapter/       agent-harness seam: AgentPort, and the Claude Code CLI
                 harness that drives it as an external process
  agent/         single-task agent runtime (tool loop), budgets, transcripts
  task/          task model, DAG, state machine, scheduler
  workflow/      concurrency, retries with feedback, resume, approvals
  builder/       incremental build subsystem: analysis, cache, planner
  planner/       YAML workflow schema + the LLM planner, one path for both
  git_manager/   worktrees, branches, merges, conflict detection
  test_runner/   gate execution and result parsing
  api/           FastAPI REST + SSE + WebSocket control plane
  auth/          users, roles, API keys, sessions, audit log
  ops/           hardening, rate limiting, backup, retention, migration
  dashboard/     single-page UI served by the API
tests/
```

Dependencies point downward only. `provider/` is the sole module permitted to
know a model vendor's wire format; `adapter/` is the sole module permitted to
know an external agent harness (the Claude Code CLI, not an API).

---

## Requirements

- Python 3.11+
- Git 2.30+ (worktree support)
- A model backend, chosen in config — any one of:
  - An Anthropic API credential (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
    or an active `ant auth login` profile) for `[provider.anthropic]`
  - A local or self-hosted OpenAI-compatible endpoint for `[provider.openai_compat]`
  - **No credential at all** — a Claude Code CLI login
    (`[provider.claude_cli]` / `[agent] adapter = "claude_code"`), billed to
    the subscription, not an API key

`pip install -e .` builds and installs from a clean checkout (`pyproject.toml`,
`Dockerfile`, `docker-compose.yml`, `scripts/install.sh` / `.ps1` all ship).

---

## License

See [`LICENSE`](LICENSE).
