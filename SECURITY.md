# Security

OrchestratorPro hands a repository to a language model and lets it write code.
That is the product, so the security posture cannot be "do not run untrusted
code" — it has to be "assume the code is untrusted and bound what it can reach".
This document says what that boundary is, where it is enforced, what an external
scan flags that we accept, and how to report something we missed.

---

## Threat model

**The agent is the untrusted input.** Not hostile by assumption, but not
trustworthy either: it is a sampled process acting on a prompt that may itself
contain repository text, issue bodies, or a previous attempt's output. Any path,
command, or diff it produces is attacker-influenceable in the same sense a form
field is. Nothing it says about its own work is evidence — including "I made the
change" and "the tests pass".

**The worktree is the sandbox boundary.** Every attempt runs in a Git worktree
the engine created for it: a throwaway checkout, never the operator's tree,
never the default branch. Work leaves that boundary only by passing the
project's own gate and then merging through Git plumbing. An attempt that is
abandoned takes its worktree with it.

**Out of scope, stated plainly.** This is a single-operator control plane run on
the operator's own machine under the operator's own model credentials. It is
not a multi-tenant service and does not defend one operator from another. A
model backend can read anything sent to it, so a prompt containing a secret has
already leaked it — which is why credentials never enter prompt assembly. And an
operator with shell access can do anything the process can; hardening is aimed
at the agent, not at the person who started it.

**What the boundary does not cover.** The `ClaudeCodeHarness`
(`orchestrator/adapter/claude_code.py`) delegates editing to an external CLI
that opens files with its own tools. Our `FilesystemPort` confinement cannot
reach inside it — the confinement there is the worktree plus the CLI's own
permission model (`--add-dir` scoped to the worktree, an explicit tool
allowlist, `Bash` withheld). `docs/030` §5.2 A-4 describes this `EXTERNAL_IO`
case and prescribes a container-backed shell for it; we accept the gap
deliberately and record it here rather than in a comment nobody reads.

---

## What is enforced, and where

| Control | Where | What it stops |
|---|---|---|
| Path confinement | `agent/tools.py::resolve_within` — resolves through symlinks, then requires `is_relative_to(root)` | Absolute paths outside the workspace, `..` traversal, and symlink escapes — including a symlinked *parent* of a file that does not exist yet |
| Executable allowlist | `agent/tools.py::check_command` | Running anything not named in advance; shell metacharacters are rejected outright rather than escaped |
| `argv`, never a shell string | every `subprocess` call site | Command injection, structurally — there is no shell to inject into (`docs/030` §5.4 rule S-1) |
| Isolation per attempt | `git_manager` worktrees | An attempt reaching another attempt's files, or the operator's checkout |
| Audited Git chokepoint | `git_manager` | Destructive operations on branches OrchestratorPro did not create |
| Gate decides, not the agent | `workflow/executor.py` | An attempt marking itself green; a conflicted merge counted as success (D-2d) |
| Parameterised SQL | `auth/store.py`, `store/` | Injection through any user- or agent-supplied value |
| Credentials never in prompts | `agent/prompt.py` assembly rules | Secrets persisting in a transcript that is written to disk |
| No invariant rests on `assert` | enforced by review + the `S101` rule below | A guarantee silently disappearing under `python -O` |

Each row has tests. The path-confinement vectors in particular are covered
one-by-one in `tests/agent/test_tools.py`.

---

## Continuous checking

Two tools run against every milestone; both are in the definition of done in
`CLAUDE.md`.

```
.venv\Scripts\python -m ruff check orchestrator/ --select S     # flake8-bandit
.venv\Scripts\python -m pip_audit -r requirements.txt           # known CVEs
```

`S` is part of the standing ruff selection in `pyproject.toml`, not a separate
pass, so a new finding fails the same lint everything else does. **Every current
finding in `orchestrator/` is either fixed or waived on its own line with a
reason** — the list below is exhaustive, which is the point: a standing pile of
unreviewed warnings trains people to ignore the next real one.

`pip-audit` reports **no known vulnerabilities** in the pinned dependency set
(last checked 2026-07-31, at `docs-drift-and-mypy-fixes` branched from `main`
@ `e86488d`). `tests/ops/test_security.py` keeps the configuration honest
offline (S is selected, every waiver carries a reason) and runs the network
audit only when `ORCHESTRATORPRO_TEST_PIP_AUDIT=1` is set, following the
opt-in convention every other `live` test uses — the default suite makes no
network calls.

---

## Accepted findings

These are flagged by automated scanning (ruff `S`, and an external Aikido SAST
run) and accepted after review. Each is waived at the line with a short reason;
the reasoning in full is here.

### `S608` — SQL built from a string, `auth/store.py:609`

The query text is assembled from **fixed literal fragments only** (`"actor = ?"`,
`"action = ?"`, `"at >= ?"`), chosen by which filters the caller supplied. Every
value is a bound `?` parameter and the `LIMIT` is clamped to an integer range.
No caller-supplied string reaches the SQL text. Rewriting it as one static query
per filter combination would be eight near-identical queries to satisfy a
pattern matcher.

### `S603` / `S607` — subprocess without a full path

Six sites: `adapter/claude_code.py` (`git status`), `provider/claude_cli.py`
(`taskkill`, and the CLI `Popen`), `test_runner/execution.py` (`taskkill`).

All pass an **`argv` list, never a shell string**, which is the documented
structural defence (`docs/030` §5.4 rule S-1) and a stronger property than the
one `S603` is asking about. `git` and `taskkill` resolve from `PATH`
deliberately: pinning an absolute path would break every operator whose Git
lives somewhere else, and an attacker who can rewrite the operator's `PATH`
already has code execution as that operator. The one path that *is* resolved
explicitly — the `claude` executable — is resolved for correctness, not
security (an npm `.cmd` shim truncates multi-line arguments).

### `S105` / `S311` — literals and randomness that are not what they look like

`auth/tokens.py` and `provider/base.py:194` contain string constants ruff reads
as hardcoded passwords; they are a token *type* name and an error code.
`task/retry.py` uses `random` for retry jitter, which is a scheduling decision,
not a secret.

### `write_text` in `ops/migrate.py:272` and `ops/backup.py:223`

Flagged as unvalidated file writes. Both are **operator-invoked CLI paths**: the
operator names the target on their own command line, running as themselves, to
write a config file or a backup manifest. There is no agent in this path and no
privilege boundary to cross — the operator can already write those files with a
text editor. Adding confinement here would be ceremony that suggests a boundary
exists where none does, which is worse than the finding. **Accepted as-is.**

### `S101` in `tests/`

Tests assert; that is what they are. `S101`, along with `S105`/`S106`
(throwaway credentials in fixtures), `S311` (random fixture data), and
`S603`/`S607` (tests drive real subprocesses on purpose) are ignored for
`tests/*` in `pyproject.toml`. They are not ignored anywhere else.

---

## Reporting a vulnerability

Report privately through **GitHub Security Advisories** on
[emailmaomao/OrchestratorPro](https://github.com/emailmaomao/OrchestratorPro/security/advisories/new)
— it is a private channel to the maintainers and does not create a public issue.
This is deliberately the only channel: a public repository should not carry a
personal address. A dedicated security mailbox belongs here once the product
has paying customers and someone is accountable for an SLA; until then,
advertising one would promise a response desk that does not exist.

Please include what you did, what happened, and what you expected. If it
involves the agent escaping the worktree boundary, a reproduction workflow YAML
is the most useful thing you can send.

Expect an acknowledgement within a week. This is a small project with no
security team and no bounty; what we can promise is that a real escape from the
worktree boundary is treated as a release blocker, not as a backlog item.
