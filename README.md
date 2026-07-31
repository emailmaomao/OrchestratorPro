# OrchestratorPro

**A robot workshop for your codebase.** It builds parts of your app while you
do something else, then hands you the finished work to inspect before anything
real changes.

---

## What this actually is, in plain words

You have a project. There is a job in it you would rather not do by hand.

You write a short recipe file saying what you want. OrchestratorPro then takes
a copy of your project into a sealed room, lets an AI agent work in there, runs
**your own tests** against whatever it produced, and throws the work away if the
tests fail. If they pass, it puts the result on a side branch and stops.

Then you look at it, the same way you would review a coworker's pull request,
and you decide whether to keep it.

**The AI never touches your real code.** It works on a copy, in a separate
folder, on a separate branch. `main` is yours alone — there is no setting to
change that.

Think of it as a robot that builds LEGO in its own room. A machine shakes the
result to see if it falls over. Only you are allowed to put anything on the
family castle.

---

## Copy-paste setup

### Part 1 — once per computer, then never again

```powershell
# 1. Make a folder to work in, and go into it
mkdir <YOUR DRIVE LETTER>:\<Your Code FolderName>   # only if the folder does not exist yet
cd    <YOUR DRIVE LETTER>:\<Your Code FolderName>   # change directory into your local folder

# 2. Get the code
git clone https://github.com/emailmaomao/OrchestratorPro.git
cd OrchestratorPro

# 3. Install it (creates its own .venv, does not touch your system Python)
.\scripts\install.ps1

# 4. Prove it installed
.\.venv\Scripts\orchestratorpro.exe version

# 5. Sign in to Claude Code (no API key needed - uses your subscription)
claude auth
```

**Sample**, with real values filled in:

```powershell
mkdir D:\AiProject
cd    D:\AiProject

git clone https://github.com/emailmaomao/OrchestratorPro.git
cd OrchestratorPro

.\scripts\install.ps1
.\.venv\Scripts\orchestratorpro.exe version
claude auth
```

Add `-Dev` to `install.ps1` **only** if you intend to work on OrchestratorPro
itself - it pulls in pytest, ruff, and mypy, which you do not need just to use
the tool. Skip `claude auth` if you are already signed in.

Then create **`C:\Users\<you>\.orchestratorpro\config.toml`** with this in it:

```toml
[agent]
adapter = "claude_code"
model   = "sonnet"

[provider.claude_cli]
executable = "claude"

[gates]
test_command = "pytest -q"
parser       = "pytest"
timeout_s    = 900.0
```

Check it took:

```powershell
.\.venv\Scripts\orchestratorpro.exe config check
```

`configuration is valid` means you are done. **This file is read automatically
from now on.** You never have to set anything in PowerShell again - and that
matters, because `$env:` variables vanish the moment you close the window.

### Part 2 - every time you want work done

```powershell
cd <YOUR DRIVE LETTER>:\<Your Code FolderName>\OrchestratorPro   # e.g. D:\AiProject\OrchestratorPro

# check the recipe is well-formed (free, instant)
.\.venv\Scripts\orchestratorpro.exe workflow check workflows\example.yaml

# do the work
.\.venv\Scripts\orchestratorpro.exe --repo <PATH TO THE APP YOU WANT WORKED ON> run workflows\example.yaml
```

> **`--repo` goes BEFORE the word `run`.** It is a global option. Putting it
> after gives you `unrecognized arguments: --repo`.

### One-time versus every-time

| Thing | How often |
|---|---|
| `install.ps1` | Once per computer |
| `config.toml` | Once per computer |
| `claude auth` | Once, until the session expires |
| Writing a recipe file | Once per job |
| The `run` command | Every time you want work done |
| Setting `$env:` variables | **Never.** The config file replaced them. |

---

## Writing a recipe

A recipe is a YAML file. A goal, and a few steps. Steps can wait for each other,
and a step that waits sees the finished work of the one before it.

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
    title: services/tag_filter.py - the logic, without the widget
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

Copy `workflows/example.yaml` and edit it. Write prompts the way you would
brief a competent stranger: say what to build, what to test, and what not to
touch.

**Always include a "wire it in" step.** A run can pass every test while leaving
the new code connected to nothing - tests only check the code they can reach.

---

## What happens when you press go

1. **A private copy per step.** A `git worktree` - a full second checkout on its
   own branch. Step 2's copy already contains step 1's finished work.
2. **An agent works in there.** Claude Code CLI, inside that folder only.
3. **Your tests run.** Not the agent's opinion of itself - your actual suite.
4. **Only passing work is kept**, committed and merged onto
   `orchestrator/<run-id>/integration`.
5. **Everything is written down** - a full JSONL transcript of every turn.

Then it stops and waits for you.

```
extract-tag-service: succeeded
  run     run_01J...
  steps   2/2 succeeded
  branch  orchestrator/run_01J.../integration
  review the diff before merging; nothing reached main
```

Review and adopt it yourself:

```powershell
cd <PATH TO THE APP YOU WANT WORKED ON>
git diff main...orchestrator/<run-id>/integration
git merge --no-ff orchestrator/<run-id>/integration
```

**Exit codes mean something**, so a scheduled job can branch on them:
`0` every step succeeded, `1` something did not, `2` the invocation was wrong,
`3` it refused as unsafe.

---

## The rules it will not break

- **It never merges to `main`.** Deliberate, and there is no flag to disable it.
- **It never pushes.** Nothing leaves your machine.
- **It never touches your working tree.** Agents only ever see their own copy.
- **Failing work is discarded**, not committed with a warning.
- **Without `--repo` it refuses to run**, rather than quietly producing
  unverified work. Pass `--allow-unverified` if a dry run is genuinely what you
  want.

---

## What your project needs

| | |
|---|---|
| **Git** | A repository with a clean tree. Isolation is the whole safety model. |
| **A test suite** | The gate *is* your tests. Set `[gates] test_command` if you do not use `pytest -q`. With no tests, nothing verifies the agent's work and this tool is not worth using. |
| **Test-shaped work** | Ask: if the agent got this wrong, would a test notice? Refactors, extractions, parsers, data transforms - yes. Anything visual - no. There is no GUI checking, so a run can pass every gate and still look broken on screen. |

---

## When something goes wrong

| Message | What it means |
|---|---|
| `unrecognized arguments: --repo` | `--repo` went after the subcommand. It goes before. |
| `refusing to run: execution mode is 'fallback'` | No usable Git repository at `--repo`. Working as intended. |
| `refusing to start: api.host ... is not loopback` | A public bind needs allowed hosts, a rate limit, and a token variable. |
| Exit code `3` | The configuration or deployment was refused as unsafe. The message names the setting. |
| Every step fails immediately | Run your test suite by hand first. If it was already red, every gate fails and it is not the agent's fault. |

---

## Running it as a server instead

Same pipeline, with a dashboard, live progress, an approval queue, and a REST
API other systems can drive:

```powershell
.\.venv\Scripts\orchestratorpro.exe --repo <PATH TO THE APP YOU WANT WORKED ON> serve
```

Dashboard at <http://127.0.0.1:8765/ui/>. The bind is loopback-only and refuses
a public address without allowed hosts, a rate limit, and a token variable.

---

## Status

**v1.0.0**, with two workstreams past it (M17, M18) also complete.

| | |
|---|---|
| Version | **1.0.0** |
| Milestones | M0-M18 complete; what comes next is in [`ROADMAP.md`](ROADMAP.md) |
| Source | 78 modules, ~30,000 lines under `orchestrator/` |
| Tests | 2,412 test functions across 85 modules |
| API | 44 paths / 54 operations, plus a WebSocket stream |

Verified against a real project, not only against fakes: eight runs have
executed this path end to end, several with real code adopted.

**Known limits, stated plainly:** there is no visual or GUI verification; the
LLM planner exists but has never been run against a real task, so you still
write the recipe yourself; worktrees and transcripts are not cleaned up
automatically; and reported token costs are estimates, not a bill.

Milestone history is in [`TASKS.md`](TASKS.md); known gaps and debt are
documented in [`TECH_DEBT.md`](TECH_DEBT.md).

---

## Documents

| Document | What it covers |
|---|---|
| [`TECH_DEBT.md`](TECH_DEBT.md) | Known gaps, debt, and where docs and code disagree. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | What is actually assembled, and the seams to extend it through. |
| [`API_STATUS.md`](API_STATUS.md) | The stable surface other systems build on, with its gaps. |
| [`ROADMAP.md`](ROADMAP.md) | What comes after v1.0. |
| [`AUTONOMY_ROADMAP.md`](AUTONOMY_ROADMAP.md) | How far this engine can run without a human, and what stands in the way. |
| [`docs/100_INSTALLATION.md`](docs/100_INSTALLATION.md) | Docker, pip, configuration layers, backups, retention. |
| [`docs/110_API_GUIDE.md`](docs/110_API_GUIDE.md) | The REST API, and why its shapes are what they are. |
| [`docs/000_PROJECT_VISION.md`](docs/000_PROJECT_VISION.md) | Why this exists, who it is for, what it refuses to be. |
| [`docs/010_REQUIREMENTS.md`](docs/010_REQUIREMENTS.md) | Numbered functional and non-functional requirements. |
| [`docs/020_ARCHITECTURE.md`](docs/020_ARCHITECTURE.md) | Layering, module contracts, data flow, concurrency. |
| [`ORCHESTRATOR_PRO_SPEC.md`](ORCHESTRATOR_PRO_SPEC.md) | The authoritative technical specification. |
| [`TASKS.md`](TASKS.md) | Milestone plan and current status. |
| [`CLAUDE.md`](CLAUDE.md) | Working agreement for AI assistants in this repo. |

---

## How it works

```
  goal + repo
      |
      v
  +--------+   plan     +------------+  schedule  +-----------+
  | builder| ---------> | task graph | ---------> | workflow  |
  +--------+            +------------+            +-----+-----+
                                                        | dispatch
                              +-------------------------+-----------------+
                              v                         v                 v
                        +----------+             +----------+      +----------+
                        |  agent   |             |  agent   |      |  agent   |
                        | worktree |             | worktree |      | worktree |
                        +----+-----+             +----+-----+      +----+-----+
                             | tests pass?            |                 |
                             +------------------------+-----------------+
                                                 |
                                                 v
                                    integration branch, for your review
```

Every task attempt gets its own `git worktree` on its own branch. Attempts never
touch your checked-out working tree, and the system never merges to `main` -
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
know an external agent harness.

---

## Requirements

- Python 3.11+
- Git 2.30+ (worktree support)
- A model backend, any one of:
  * An Anthropic API credential for `[provider.anthropic]`
  * A local or self-hosted OpenAI-compatible endpoint for `[provider.openai_compat]`
  * **No credential at all** - a Claude Code CLI login (`[agent] adapter =
    "claude_code"`), billed to the subscription rather than an API key

---

## License

MIT. See [`LICENSE`](LICENSE).
