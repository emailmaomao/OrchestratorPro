# CLAUDE.md — working agreement for this repository

Read this before writing code in OrchestratorPro. It encodes decisions already
made, so they do not get re-litigated or accidentally reversed.

---

## Read order

1. `ORCHESTRATOR_PRO_SPEC.md` — authoritative.
2. `TASKS.md` — what is done, what is next.
3. `docs/` — vision, requirements, architecture.

If the spec and the code disagree, **the spec wins**. If the spec is wrong, amend
it in the same commit that changes the code — never leave them diverged.

---

## Process rules

- **Work in milestones.** One milestone at a time, in the order given in
  `TASKS.md`. Do not skip ahead because a later milestone looks easier.
- **At most 5 source files per response.** Documentation and test files that
  accompany those sources do not count against the limit, but do not use that as
  a loophole to land a whole milestone in one turn.
- **Never overwrite or regenerate an existing file** unless the change is the
  point of the task. Prefer targeted edits. Read before you write.
- **Run `pytest -q` before marking a milestone complete.** Green means green — a
  skipped test that hides a failure does not count.
- **Run the security checks before marking a milestone complete**, and treat a
  finding as blocking:

  ```
  .venv\Scripts\python -m ruff check orchestrator/ --select S    # flake8-bandit
  .venv\Scripts\python -m pip_audit -r requirements.txt          # known CVEs
  ```

  Fix it, or waive it **on its own line with a reason** and add it to the
  accepted-findings list in `SECURITY.md`. A standing pile of unreviewed
  warnings is how the next real one gets missed. `tests/ops/test_security.py`
  keeps this honest: it fails if `S` leaves the lint selection, if a waiver
  gives no reason, or if an `assert` reappears in `orchestrator/`.
- **Commit after every completed milestone**, and update `TASKS.md` in that same
  commit. The commit message names the milestone.
- **Report honestly.** If tests fail, say so and paste the output. If part of a
  milestone is blocked, finish everything else and state plainly what was left
  and why. Scaling work down is the operator's call, not yours.

---

## Stack

| | |
|---|---|
| Language | Python 3.11+ |
| Async | `asyncio` throughout; no threads except where a library forces it |
| Web | FastAPI + Uvicorn |
| Persistence | SQLite via the standard library's `sqlite3` |
| Validation | Pydantic v2 at the HTTP boundary; frozen dataclasses everywhere else |
| Config | TOML via `tomllib` |
| Tests | pytest; async code is driven with `asyncio.run` (no pytest-asyncio) |
| Lint/format | ruff (lint + format), mypy in strict mode |

Dependencies live in `.venv` and are pinned in `requirements.txt`. Layers L0–L3
import nothing outside the standard library; FastAPI, Uvicorn, and Pydantic are
reachable only from `orchestrator/api/`. Run the suite with
`.venv\Scripts\python -m pytest -q`.

---

## Code conventions

- **Type everything.** `mypy --strict` must pass. No bare `Any` at module
  boundaries; if you need one internally, comment why.
- **Dataclasses for domain objects**, frozen where the object is a value.
  Pydantic models only where data crosses a trust boundary (API, config, LLM
  structured output).
- **`Protocol` for pluggable seams** — `Provider`, `Adapter`, `GateRunner`. No
  ABCs, no inheritance hierarchies deeper than one level.
- **Errors are typed and carry a stable `code`.** Every custom exception derives
  from `OrchestratorError` and declares `retryable`.
- **No `print`.** Structured logging via `orchestrator.core.logging`.
- **Async-first.** Blocking calls (subprocess, Git, file I/O over a few KB) go
  through `asyncio.to_thread` or an async library. A blocking call on the event
  loop is a bug.
- **Module layering is enforced.** A module imports from its own layer and below
  only. Layer order is in the spec, §4. A layering violation is a review reject.
- Match the surrounding code's comment density and idiom. Comments state
  constraints the code cannot express — not what the next line does.

---

## LLM integration rules

These prevent HTTP 400s. They are not stylistic.

- **The default model is `claude-opus-5`.** Do not substitute a cheaper model to
  save cost; that is the operator's decision, expressed in config.
- **Only `orchestrator/provider/` may import `anthropic`.** Only
  `orchestrator/adapter/agent_sdk.py` may import `claude_agent_sdk`. These are
  different packages with different purposes — the Agent SDK is Claude Code as a
  library; the API SDK's tool runner is a loop helper. They are not
  interchangeable.
- **Never send `temperature`, `top_p`, or `top_k`.** Removed on this model
  family; any value returns 400. Steer behavior by prompting.
- **Never send `thinking.budget_tokens`.** Removed; returns 400. Use
  `thinking={"type": "adaptive"}` and control depth with
  `output_config={"effort": ...}`.
- On `claude-opus-5`, thinking is **on by default** when `thinking` is omitted,
  and `thinking={"type": "disabled"}` is valid **only at effort `high` or below**
  — pairing it with `xhigh`/`max` returns 400. Validate that pairing client-side.
- `max_tokens` caps thinking **plus** output. **Stream** any request above
  ~16 000 `max_tokens` or the HTTP timeout fires.
- **No assistant-turn prefills** — they return 400. Use
  `output_config={"format": {"type": "json_schema", ...}}` for structured output.
- **Check `stop_reason` before reading `content`.** A refusal is a successful
  HTTP 200 with `stop_reason == "refusal"`; indexing `content[0]` unconditionally
  will crash on it.
- The config key `agent.budget_tokens` is **our own spend cap** and must never
  reach a request body. The name collision with the removed API parameter is
  known; keep them separate in code.

When touching anything model-related, load the `claude-api` skill rather than
working from memory. These details change.

---

## Prompt assembly rules

Caching is a prefix match — any byte change invalidates everything downstream.

- **No `datetime.now()`, UUIDs, or run identifiers in system prompts.** Dynamic
  context goes late, in message content.
- Serialize JSON with sorted keys; sort tool lists by name. Non-deterministic
  serialization silently destroys the cache.
- Do not change the tool set mid-conversation. Convey modes as message content.
- There is a unit test that renders a prompt twice and compares hashes. If you
  break determinism, it will catch you.

---

## Security invariants

Treat agent output as untrusted input to the harness — always.

- Canonicalize every model-supplied path and verify containment in the workspace
  root before any I/O. Reject `..`, symlink escapes, and outside-root absolutes.
- `run_command` uses an executable **allowlist** and rejects shell metacharacters.
  A blocklist is not sufficient.
- Never interpolate credentials into prompts or messages — they persist in the
  transcript.
- All destructive Git operations go through the single audited chokepoint in
  `git_manager`, and refuse to touch branches OrchestratorPro did not create.

---

## Testing

- Unit tests colocated by module under `tests/`, mirroring the package tree.
- **No live API calls in the default suite.** Use recorded fixtures. Live tests
  are marked `@pytest.mark.live` and are never required for a milestone.
- `git_manager` tests run against real temporary repositories. Mocking Git would
  test nothing.
- The scheduler gets property tests over randomly generated DAGs.

---

## Commit conventions

```
M3: task model and DAG scheduler

Adds Task/TaskGraph domain objects, the task state machine, and the
pure-function scheduler with cycle detection.

Tests: 47 passed.
```

Branch first if on `main`. Commit or push only when asked.
