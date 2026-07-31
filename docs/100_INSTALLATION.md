# Installation

OrchestratorPro runs on your machine or your server. There is no hosted service
and nothing phones home.

Three ways to install it, in the order most people should try them.

---

## Before you start

| | |
|---|---|
| Python | 3.11 or newer |
| Git | Required to run agents (each attempt gets a worktree). Optional otherwise. |
| Disk | The event log grows with use. A busy month is tens of megabytes. |
| Network | Only to whatever model backend you configure. |

**This build binds to `127.0.0.1` and refuses any other address unless you have
configured allowed hosts, a rate limit, and a token variable.** That refusal is
deliberate and it is checked at start-up, not at first request.

---

## 1. Docker Compose — the shortest path

```bash
git clone https://github.com/emailmaomao/OrchestratorPro.git
cd OrchestratorPro
cp .env.example .env      # then read it; several defaults are deliberate
docker compose up -d
```

Open <http://127.0.0.1:8765/ui/>.

The port is published on loopback only. The event log lives on a named volume,
so `docker compose down` does not lose your runs; `docker compose down -v`
does.

Take a backup:

```bash
docker compose --profile backup run --rm backup
```

That writes a verified snapshot into `./backups` and prunes to the newest seven.
It is a profile rather than a running service because your host already has a
scheduler, and a container that idles in a loop is one more thing to keep alive.

---

## 2. Install script — a virtual environment on this machine

**Linux and macOS**

```bash
./scripts/install.sh          # into ./.venv
./scripts/install.sh --dev    # with the test and lint tooling
```

**Windows**

```powershell
.\scripts\install.ps1
.\scripts\install.ps1 -Dev
```

Both create a virtual environment rather than touching the Python your machine
uses for everything else, check the interpreter version by asking it rather than
by parsing `--version`, and verify the install actually runs before reporting
success.

Afterwards:

```bash
.venv/bin/orchestratorpro config check
.venv/bin/orchestratorpro serve
```

---

## 3. pip — into an environment you manage

```bash
python -m venv .venv
.venv/bin/pip install orchestratorpro          # or: pip install -e .
.venv/bin/orchestratorpro version
```

The wheel carries the dashboard assets. If `/ui/` 404s from an installed copy,
the package data did not ship — that is a packaging bug, not a configuration
problem.

---

## First run

### Check the configuration before serving

```bash
orchestratorpro config check
```

Exit `0` means valid. Exit `3` means the deployment would be unsafe and the
message says why. Warnings are printed but do not fail the check — `auto_approve`
and any CORS origin are reported as high severity because both let something
other than you drive your agents.

### Require credentials

A fresh installation has **no accounts**, and serves every route as a built-in
operator. That is right for one person on loopback and wrong for anything else.
To lock it down:

```bash
orchestratorpro auth bootstrap
```

This prints a generated password **once**. It refuses to run a second time —
otherwise it would be a way to add an administrator without being one. From that
moment every route requires a credential.

Add people:

```bash
orchestratorpro auth add alice --role operator
orchestratorpro auth add ci-readonly --role viewer
orchestratorpro auth users
```

There are three roles and they are ordered: `viewer` reads, `operator` starts
and stops work, `admin` manages accounts and reads the audit trail.

### Point it at a model

OrchestratorPro is provider-independent. It records, plans, and reports with no
backend at all, and refuses to execute rather than guessing at one.

```bash
# Anthropic
export ANTHROPIC_API_KEY=...
export ORCHESTRATORPRO__PROVIDER__ANTHROPIC__MODEL=claude-opus-5

# or a self-hosted OpenAI-compatible endpoint
export ORCHESTRATORPRO__PROVIDER__OPENAI_COMPAT__MODEL=llama3.1:70b

# or a self-hosted Hermes backend
export ORCHESTRATORPRO__PROVIDER__HERMES__MODEL=...

# or the Claude Code CLI, subscription-billed — no API key anywhere.
# Requires `claude` on PATH and an active `claude /login` session; the
# agent's model comes from [agent] model (below), not from this block.
export ORCHESTRATORPRO__PROVIDER__CLAUDE_CLI__EXECUTABLE=claude
```

**The Claude Code CLI path (M18) is worth calling out specifically**, since
it's the one deployment mode that needs no credential of any kind in this
file, this environment, or anywhere else: no `ANTHROPIC_API_KEY`, no token, no
config secret. Set `[agent] adapter = "claude_code"` (or the matching env var)
so served runs drive the CLI as an external harness instead of the in-process
tool loop; `[agent] model` (default `"sonnet"`, an alias the CLI resolves, not
a pinned model id) picks which model the harness asks for, separately from
whichever provider is bound for other model calls.

**Credentials are never configuration.** Any config key whose name looks like a
secret is rejected at load time. Set the value in the environment; put only the
*name* of the variable in configuration.

---

## Configuration

Four layers, each overriding the last:

1. Built-in defaults
2. `~/.orchestratorpro/config.toml`
3. `orchestrator.toml` in the repository
4. `ORCHESTRATORPRO__*` environment variables

The environment wins because it is the layer a deployment controls without
rebuilding anything:

```bash
ORCHESTRATORPRO__API__PORT=8080          # [api] port = 8080
ORCHESTRATORPRO__RUN__MAX_CONCURRENCY=8  # [run] max_concurrency = 8
```

Two blocks that matter for a served run and are easy to miss because no
example above names them:

```toml
# [gates] — what a served run's --repo mode checks before accepting an
# attempt's work. Without this, the [gates] default (pytest -q) applies.
[gates]
test_command = "pytest -q"
parser       = "pytest"       # or "junit_xml" / "exit_code"
timeout_s    = 900.0

# [agent] — the agent's own settings, separate from [provider.*] above.
[agent]
budget_seconds    = 1800
budget_tokens     = 2000000
budget_tool_calls = 200
adapter           = "tool_loop"   # or "claude_code" for the CLI harness
model             = "sonnet"      # the agent's model; "" = let the CLI decide
```

Two underscores separate the levels — one would be ambiguous against keys that
already contain an underscore. An empty variable means "not set", so a blank
line in `.env` cannot override a real file.

Validation is strict: an unknown key is an error, not a warning. A typo in
`max_concurency` that silently kept the default would be discovered by noticing
the run was slow.

### Upgrading a configuration file

Because validation is strict, a renamed key breaks every existing installation
on upgrade. That is the other half of the bargain:

```bash
orchestratorpro config migrate ~/.orchestratorpro/config.toml          # preview
orchestratorpro config migrate ~/.orchestratorpro/config.toml --write  # apply
```

It previews by default, keeps the original beside the new one, and reports what
it moved rather than dropping it.

---

## Backups

The event log is the system's memory. Lose it and every run becomes
unrecoverable, whatever is still in a worktree.

```bash
orchestratorpro backup create ./backups --label pre-upgrade
orchestratorpro backup list ./backups
orchestratorpro backup verify ./backups/orchestrator-*.db
orchestratorpro backup restore ./backups/orchestrator-....db --yes
orchestratorpro backup prune ./backups --keep 7 --yes
```

Snapshots use SQLite's online backup API, so they are consistent even while the
database is being written. Each carries a manifest with a digest, a schema
version, and how many events it holds — a backup nobody can verify is a backup
nobody should rely on. `restore` verifies before touching anything and moves the
existing database aside rather than deleting it.

---

## Retention

Finished runs can be archived out of the live database and brought back:

```bash
orchestratorpro retention plan                               # what would go
orchestratorpro retention apply ./archives --yes             # archive and prune
orchestratorpro retention verify ./archives                  # check them all
```

A run is only eligible when it is finished, older than `--keep-days`, **and**
outside the newest `--keep-runs`. All three, because any one alone has an
obvious failure mode. Nothing is pruned that was not archived and then read back
successfully first.

**This covers runs only — worktrees and transcripts are not retained by
anything above.** They accumulate without limit on disk (`Q5` in the spec's
open questions, tracked as `OP-009`). On a long-lived deployment, plan to
clean `<workspace>/worktrees/` and `<db-dir>/transcripts/` by hand until that
lands.

---

## Troubleshooting

**`refusing to start: api.host = '0.0.0.0' is not loopback`**
Working as intended. A public bind needs `security.allowed_hosts`,
`security.rate_limit_per_minute` above zero, and `api.auth_token_env` naming a
variable that is set. Or put an authenticating proxy in front and keep the bind
on loopback.

**`/ui/` returns 404 from a pip install**
The dashboard assets did not ship with the wheel. Reinstall from a wheel built
by this repository's `pyproject.toml`.

**Every request returns 401 after `auth bootstrap`**
Expected. Log in at `POST /auth/login` and send `Authorization: Bearer <token>`.
The dashboard does this for you.

**`unrecognized arguments: --database`**
Global options come before the subcommand:
`orchestratorpro --database path/to.db serve`, not the other way round.

**The container exits immediately**
`docker compose logs orchestratorpro`. An exit code of 3 is the deployment
check refusing an unsafe configuration, and the message names the setting.
