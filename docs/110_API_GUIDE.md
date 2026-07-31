# API guide

The control plane is a REST API with two streaming transports. The dashboard is
a client of it and uses nothing else, which is the main reason to trust that the
API is complete.

`GET /openapi.json` is generated from the code and is authoritative. This guide
explains the parts that a schema cannot: what the shapes mean, and why some of
them are the way they are.

---

## Conventions

### Errors

Every failure has one shape:

```json
{
  "error": {
    "code": "not_found",
    "message": "no run with id run_01J...",
    "retryable": false,
    "detail": {"run_id": "run_01J..."}
  }
}
```

Branch on `code`, never on the message. Codes are stable; messages are written
for people.

| Status | Meaning |
|---|---|
| 400 | The request violated a domain rule, or an identifier was malformed. |
| 401 | No usable credential. |
| 403 | A credential, but not enough role. |
| 404 | The addressed thing does not exist. |
| 409 | The request contradicts the current state. |
| 413 | The body exceeded the configured ceiling. |
| 422 | The body or parameters did not validate. |
| 429 | Rate limited. `Retry-After` says how long. |
| 503 | The server is not configured to do this — usually no execution backend. |

`retryable` is a fact about the operation, not a suggestion. A 503 from a
missing backend is retryable only after somebody configures one.

### Reads replay, writes append

`GET /runs/{id}` reconstructs the run from its event log rather than reading the
materialized tables. It is slower and it cannot disagree with the record.

Every write is an event. Amending a task appends a second `task.created`;
retiring one appends `task.abandoned`. Nothing is edited, so anything the API
does survives a crash and shows up in a replay.

### Authentication

An installation with **no accounts** serves every route as a built-in operator.
The moment one account exists, every route requires a credential — there is no
window where some routes are protected and others are not.

```bash
curl -s localhost:8765/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":"..."}'
```

```json
{"access_token": "eyJ...", "refresh_token": "eyJ...", "token_type": "Bearer", "expires_in": 900}
```

Send it as `Authorization: Bearer <access_token>`. An API key goes in the same
header and is recognized by its `opk_` prefix, so a client does not have to know
which kind of credential it holds.

Access tokens are short-lived and are not checked against the database, so their
lifetime is the window in which a revoked account still works. Refresh tokens
are long-lived and *are* checked, so a logout takes effect immediately.

**Streaming endpoints accept `?token=` instead**, because a browser cannot set a
header on an `EventSource` or a `WebSocket`. That exemption is limited to
`/events`, `/runs/{id}/events`, and `/runs/{id}/ws` — a token in a URL is a
token in a proxy log.

### Roles

| Role | May |
|---|---|
| `viewer` | Read runs, tasks, builds, events, config. |
| `operator` | Everything a viewer may, plus start, cancel, resume, approve, build. |
| `admin` | Everything, plus accounts, keys, and the audit trail. |

Each route declares the *least* role that may reach it. The check is `>=`.

---

## Runs

```
POST   /runs                 declare a run
GET    /runs                 list, newest first
GET    /runs/{id}            full detail, replayed
GET    /runs/{id}/status     compact progress, for polling
POST   /runs/{id}/cancel     stop starting new work
POST   /runs/{id}/resume     continue from the log
GET    /runs/{id}/log        the recorded events, paged
```

`GET /runs/{id}/status` is the one to poll:

```json
{
  "id": "run_01J...",
  "active": false,
  "total": 4, "succeeded": 3, "failed": 1, "blocked": 0,
  "percent": 100.0,
  "complete": true,
  "healthy": false,
  "summary": "4/4 steps (100%), 1 failed"
}
```

**`complete` and `healthy` are different questions.** `complete` means every
step reached a terminal state; `healthy` means none of them failed. A run can be
complete and unhealthy, and conflating the two is the single most tempting
mistake a client of this API can make.

Cancelling a run does **not** finish it. A cancelled run is recorded as
cancelled, not finished, precisely so it can be resumed later — cancel, fix
something, carry on is the most ordinary recovery there is.

---

## Tasks

```
POST   /runs/{id}/tasks              add one
GET    /runs/{id}/tasks?state=...    list, filterable
GET    /runs/{id}/tasks/{task_id}
PATCH  /runs/{id}/tasks/{task_id}    amend one that has not started
DELETE /runs/{id}/tasks/{task_id}    retire one that has not started
```

`PATCH` appends a fresh declaration rather than editing the old one, and refuses
once an attempt has begun — by then the prompt is part of an attempt's
transcript, and changing it would make the record describe work nobody asked
for.

`DELETE` cannot mean erase. It abandons the task, which stays listed. The log is
append-only.

---

## Workflows

```
POST   /workflows               register a definition
GET    /workflows               list
GET    /workflows/{name}        detail, including the compiled graph
DELETE /workflows/{name}        unregister
POST   /workflows/{name}/runs   execute; returns 202 with a run id
```

Registration validates and compiles: an unknown dependency, a duplicate step
name, or a cycle is a 400 here rather than a deadlock later. The response
carries `layers` — the waves that will run in parallel — so a client can show
the shape before spending anything.

Starting a run returns `202` immediately with the run identifier. Follow it with
`/runs/{id}/status` or `/runs/{id}/events`.

---

## Approvals

```
GET    /approvals                                         everything waiting
GET    /runs/{id}/approvals                               one run's requests
POST   /runs/{id}/tasks/{task_id}/approval                ask for review
POST   /runs/{id}/tasks/{task_id}/approval/resolve        decide
GET    /runs/{id}/tasks/{task_id}/attempts                attempt history
GET    /runs/{id}/tasks/{task_id}/transcript              what an attempt did
GET    /runs/{id}/tasks/{task_id}/diff                    what it changed
```

A decision is `approved`, `rejected`, or `retry`. Rejecting abandons the task;
retrying puts it back; approving lets the engine carry on.

**The actor comes from the credential, never from the body.** An approval
attributed to whoever the caller says they are is not attributable at all.

The queue is oldest-first, because a queue sorted newest-first has a bottom
nobody looks at.

`attempts` includes `repeated_failures` — gates that failed in more than one
attempt. At attempt three that is the thing a reviewer most wants to know: is
this the same wall, or a different one.

Each attempt also carries what actually happened to its work, from
`attempt.finished` (OP-011): **`merged`**, **`merge_status`**,
**`changed_files`**, and **`conflicted_paths`**. `merged` is `null` — not
`false` — for a log written before this field existed; that is "the log does
not say", not "did not merge". And each carries **`tokens_estimated`** (OP-004):
`true` means the number is an approximation (roughly four characters per
token), `false` means it came from the backend's own accounting. A run's
top-level `usage.tokens_estimated` is `true` if *any* contributing attempt's
tokens were estimated — one estimate makes the total an estimate. A backend
with no cost reporting (`claude_cli`) also carries a **`notional_cost_usd`**
alongside `cost_usd: null` — a scale estimate charged to nobody, not a bill.

---

## Builds

```
POST   /builds/analyze   scan a project into units and their graph
POST   /builds/plan      what would be rebuilt, and why
POST   /builds           plan and execute; returns 202
GET    /builds           history
GET    /builds/{id}      one report, with per-unit diagnostics
DELETE /builds/cache     forget every cached build
```

Every unit in a plan carries a reason (`source_changed`, `dependency_rebuilt`,
`not_cached`, `artifact_missing`, `forced`). A build system that rebuilds the
world and cannot say why is one people stop trusting.

A failing build reports located diagnostics — file, line, severity — not a wall
of log. Note that a build tool that *broke* is `errored`, not `failed`: those
are different, and treating them alike sends the next attempt to rewrite code
that compiles perfectly well.

---

## Agents

```
GET  /agents/roles     resolved model, effort, and budgets per role
GET  /agents/tools     the tool surface, with schemas
POST /agents/prompt    render a prompt without calling a model
```

`POST /agents/prompt` returns the cached prefix, the opening turn, and a
`fingerprint` over the prefix. Render identical inputs twice: if the fingerprint
changes, prompt caching has silently stopped happening, and this is the only
place to see that before the bill arrives.

Retry feedback deliberately appears in `messages`, not `blocks` — it changes
every attempt, and in the prefix it would invalidate the cache each time.

---

## Events

```
GET  /runs/{id}/log?limit=&after=   read history, paged
GET  /runs/{id}/events              SSE: replay, then live
GET  /events                        SSE: every run
WS   /runs/{id}/ws                  the same over a socket
```

Use `/log` for history and the streams for watching. A client that only wants
the past should not hold a connection open and guess when the replay ended.

The subscription is opened *before* the log is replayed and de-duplicated by
event id, so nothing is lost in the seam. A client that stops reading is
disconnected with an explicit notice rather than served a silently partial
stream.

```javascript
const stream = new EventSource(`/runs/${id}/events?token=${accessToken}`);
stream.onmessage = (message) => console.log(JSON.parse(message.data));
```

---

## Accounts and audit

```
POST   /auth/login /auth/refresh /auth/logout /auth/logout-all
GET    /auth/me /auth/sessions
GET    /auth/users            POST /auth/users            (admin)
PUT    /auth/users/{u}/password|role|active
DELETE /auth/users/{u}        (admin)
GET    /auth/keys             POST /auth/keys             DELETE /auth/keys/{id}
GET    /auth/audit            (admin)
```

An API key's secret appears in exactly one response and is never stored in a
form anything can present. A lost key is replaced, not recovered.

A key may be weaker than its account but never stronger — a key that outranks
its owner is a privilege escalation wearing a convenience.

The audit trail is append-only at the database level: there is no endpoint that
edits it and no SQL that could.

---

## System

```
GET /health   liveness, and what this server can do
GET /config   the effective configuration
```

`/health` reports `execution_available`. A server without an execution backend
records and reports but refuses to run anything, and says so with a 503 rather
than a 500 — the request was fine, the server is not equipped.

Credentials never appear in `/config`, because the config layer rejects them at
load time. `auth_token_env` is the *name* of a variable and is safe to publish.
