# API_STATUS — OrchestratorPro

**The stable surface other systems build on.** Hermes and one downstream application
consume this API and nothing else — no imports across repository boundaries.

Verified 2026-07-31 against `main` @ `e86488d` by generating the OpenAPI document
from the running application: **44 paths, 54 operations**, plus one WebSocket
route that OpenAPI does not describe.

`GET /openapi.json` is authoritative for shapes. This file records **status,
stability, and gaps** — which a schema cannot express.

**Legend** — ✅ works · 🟡 works with a caveat · 🚧 blocked by a defect ·
❌ needed and absent

---

## 1. Execution works, full pipeline, when a repository is given

`3f59746` wired `state.executor_factory` in `cli.py::cmd_serve`. Verified end to
end by `tests/e2e/test_serve_execution.py`, which drives the real `cmd_serve`
path with a scripted transport:

```
health.execution_available  →  true
POST /workflows             →  201
POST /workflows/smoke/runs  →  202  → run completes: 1 succeeded, 0 failed
```

**The pipeline behind it depends on the serve mode** (`execution_mode` in the
dry-run report):

| Mode | What a consumer gets |
|---|---|
| `full` (`serve --repo <git repo>`) | Worktree per attempt, commits, the `[gates]` suite gating acceptance, merges to `orchestrator/<run>/integration`. `GET .../diff` is meaningful; `gate.evaluated` events carry verdicts attributed to attempts |
| `fallback` (no repository) | A shared plain directory; nothing verified or committed; **`max_concurrency` clamped to 1** server-side, visible in `GET /config` |
| `unavailable` | 503 on execution; recording, replay, approvals, auth all work |

**Decision — what `execution_available` means.** It means *an executor is
wired and its provider started healthy*: `registry.startup_all()` runs at serve
time, so a provider that cannot even construct (missing SDK, broken transport)
lands the server in `unavailable` mode rather than failing every attempt.
It still does **not** promise a live completion will succeed — startup is
construction and local health, never a network call. `serve --verify` is the
opt-in strict mode: exit non-zero instead of serving degraded. Treat a healthy
first attempt, not any flag, as proof of a live backend.

Verified end to end in `tests/e2e/test_serve_execution.py`.

---

## 2. Conventions

**Errors.** One shape, always:

```json
{"error": {"code": "not_found", "message": "…", "retryable": false, "detail": {}}}
```

Branch on `code`; codes are stable, messages are for people. `retryable` is a
fact about the operation, not a suggestion.

| Status | Meaning |
|---|---|
| 400 | Violated a domain rule, or a malformed identifier |
| 401 / 403 | No usable credential / not enough role |
| 404 / 409 | Does not exist / contradicts current state |
| 413 / 422 | Body too large / did not validate |
| 429 | Rate limited; `Retry-After` says how long |
| 503 | The server is not equipped — usually no execution backend |

**Reads replay, writes append.** `GET /runs/{id}` reconstructs from the event log
rather than reading materialized tables: slower, and it cannot disagree with the
record. `PATCH` appends a fresh declaration; `DELETE` abandons rather than erases.

**Roles.** `viewer < operator < admin`, compared with `>=`. The whole router
carries a viewer floor, so a new endpoint added without thought is read-only.
With **no accounts**, every route serves as an operator and the CLI says so on
every start.

**Streaming auth.** `?token=` is accepted on `/events`, `/runs/{id}/events`, and
`/runs/{id}/ws` only — a browser cannot set a header on `EventSource` or
`WebSocket`. Anywhere else it would land in a proxy log for nothing.

---

## 3. Endpoint status

### System
| Method | Path | Role | Status |
|---|---|---|---|
| GET | `/health` | viewer | ✅ reports `execution_available` — `true` in `full`/`fallback` mode, `false` only in `unavailable` mode (§1) |
| GET | `/config` | viewer | ✅ credentials never appear; rejected at load time |

### Runs
| Method | Path | Role | Status |
|---|---|---|---|
| POST | `/runs` | operator | ✅ |
| GET | `/runs` | viewer | ✅ newest first |
| GET | `/runs/{id}` | viewer | ✅ replayed |
| GET | `/runs/{id}/status` | viewer | ✅ `complete` and `healthy` are different questions |
| POST | `/runs/{id}/cancel` | operator | ✅ cancel ≠ finish, so it stays resumable |
| POST | `/runs/{id}/resume` | operator | ✅ |
| GET | `/runs/{id}/log` | viewer | ✅ paged |

### Tasks
| Method | Path | Role | Status |
|---|---|---|---|
| POST | `/runs/{id}/tasks` | operator | ✅ |
| GET | `/runs/{id}/tasks` | viewer | ✅ filterable by state |
| GET | `/runs/{id}/tasks/{task_id}` | viewer | ✅ |
| PATCH | `/runs/{id}/tasks/{task_id}` | operator | ✅ refuses once an attempt has begun |
| DELETE | `/runs/{id}/tasks/{task_id}` | operator | ✅ abandons; the record stays |

### Workflows
| Method | Path | Role | Status |
|---|---|---|---|
| POST | `/workflows` | operator | ✅ validates and compiles; a cycle is a 400 here, not a deadlock later |
| GET | `/workflows` · `/workflows/{name}` | viewer | ✅ detail carries `layers` |
| DELETE | `/workflows/{name}` | operator | ✅ |
| POST | `/workflows/{name}/runs` | operator | ✅ executes; worktree isolation and gating apply in `full` mode, neither in `fallback` (§1) |

### Approvals and review
| Method | Path | Role | Status |
|---|---|---|---|
| GET | `/approvals` · `/runs/{id}/approvals` | viewer | ✅ oldest first |
| POST | `.../tasks/{t}/approval` | operator | ✅ |
| POST | `.../tasks/{t}/approval/resolve` | operator | ✅ actor from the credential, never the body |
| GET | `.../tasks/{t}/attempts` | viewer | ✅ includes `repeated_failures`; each attempt carries `merged` / `merge_status` / `changed_files` / `conflicted_paths` (OP-011) |
| GET | `.../tasks/{t}/transcript` | viewer | ✅ |
| GET | `.../tasks/{t}/diff` | viewer | 🟡 **Git-only.** A non-repo task has no diff |

### Builds
| Method | Path | Role | Status |
|---|---|---|---|
| POST | `/builds/analyze` · `/builds/plan` | operator | ✅ every unit carries a reason |
| POST | `/builds` | operator | ✅ |
| GET | `/builds` · `/builds/{id}` | viewer | ✅ located diagnostics |
| DELETE | `/builds/cache` | operator | ✅ |

### Agents
| Method | Path | Role | Status |
|---|---|---|---|
| GET | `/agents/roles` | viewer | ✅ resolved model, effort, budgets |
| GET | `/agents/tools` | viewer | 🟡 the **global** registry; not per-run |
| POST | `/agents/prompt` | operator | ✅ returns a `fingerprint` over the cached prefix — the only place to see caching break before the bill |

### Events
| Method | Path | Role | Status |
|---|---|---|---|
| GET | `/events` (SSE) | viewer | ✅ every run |
| GET | `/runs/{id}/events` (SSE) | viewer | ✅ subscribe-then-replay, de-duplicated by id |
| WS | `/runs/{id}/ws` | viewer | ✅ `api/routes.py:1038`; absent from OpenAPI by nature |

### Auth — 17 endpoints under `/auth`
`login`, `refresh`, `logout`, `logout-all`, `me`, `sessions`, `users` (CRUD),
`users/{u}/password|role|active`, `keys` (list/mint/revoke), `audit`. ✅
An API key's secret appears in exactly one response; a key may be weaker than its
account but never stronger; the audit trail is append-only at the database level.

---

## 4. Gaps that matter to a consumer

| # | Gap | Consequence | Priority |
|---|---|---|---|
| **A1b** | ~~`startup_all()` never called~~ — resolved: providers start (and health-check) at serve time; `serve --verify` refuses to serve degraded. Remaining truth: startup is local, so a bad *credential* still surfaces at the first live call | | done |
| **A2** | **No artifacts endpoint.** A task that produces *files* rather than a commit has no way to return them. `GET .../diff` is Git-only | Any non-repo work is unreachable through the API | **P1** |
| **A3** | `repo_path` is effectively required; no `workspace_mode` | The workspace-less mode that `ExecutionServices` already supports is not expressible over HTTP | **P1** |
| **A4** | **No gate registry.** `gates: [tests]` resolves only to the built-in test runner; nothing outside can register a `GateRunner` | A consumer cannot gate on its own rules — the single most valuable thing this engine could offer | **P1** |
| **A5** | Tool surface is global, not per-run; no registration path from outside | Every run gets the same four built-in tools | **P2** |
| **A6** | ~~Estimates presented as measurements~~ — **resolved (OP-004).** Run-detail `usage` and each attempts entry carry `tokens_estimated`; totals are sticky (one estimated contribution marks the total); the token budget axis still binds but a budget-exhausted verdict says "(token counts are estimates)"; the dashboard prefixes `~`. Found and fixed underneath: `attempt.finished` carried no usage at all, so every served attempt had replayed with **zero** tokens (FR-5.4 broken end to end). Payload additions are additive; older logs replay unchanged | | done |
| **A12** | ~~A healthy run's log did not say whether the work merged~~ — **resolved (OP-011).** `attempt.finished` now carries `merged`, `merge_status`, `changed_files`, and `conflicted_paths`, so a run's merge story is reconstructible from events alone with no Git access. Additive keys; `merged` replays as `null` ("the log does not say") for older logs rather than collapsing to `false` | | done |
| **A7** | No incremental assistant output on the stream | A client sees "running" and then a result; no progress | **P3** |
| **A8** | No per-task or per-run cost ceiling | Nothing bounds an unattended overnight run | **P2** |
| **A9** | No scheduling endpoints | Continuous development has no trigger | **P2** |
| **A10** | No cross-run memory endpoints | Each run starts cold | **P3** |
| **A11** | Approval queue, transcripts, and diffs have no dashboard page | A reviewer must use curl | **P3** |

---

## 5. Stability contract

Consumers may rely on these. Breaking any of them is a major version.

1. The error envelope, and every `code` in it.
2. Role ordering and the viewer floor.
3. `POST /workflows` → `POST /workflows/{name}/runs` → `GET /runs/{id}/status`
   → `GET /runs/{id}/events` as the primary flow.
4. `complete` and `healthy` meaning different things.
5. `cancel` recording a run as cancelled, not finished, so it stays resumable.
6. Reads replaying the log; writes appending.
7. `?token=` on streaming endpoints only.
8. `cost_usd` being `null` — never `0.0` — when a provider cannot price a call.

**Additive changes are safe:** new endpoints, new optional request fields, new
response fields. Consumers must ignore unknown response fields.

**Versioning.** `/health` reports the distribution version
(`orchestrator/core/version.py`), matched to `pyproject.toml` by a test. A
consumer should check it at startup and refuse or warn below its minimum. There
is no URL version prefix and none is planned; the envelope and the flow above are
the contract.
