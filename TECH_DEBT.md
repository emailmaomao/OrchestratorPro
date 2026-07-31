# TECH_DEBT — OrchestratorPro

New file. `OP-*` items were previously tracked only in an internal queue's "Queued
next" table with no per-line detail; this is where that detail lives once an
item needs more than a one-line queue entry. That queue still owns
sequencing and status; this file owns the specifics.

---

## OP-008 — `mypy --strict -p orchestrator`: 46 → 0

**Baseline, reverified 2026-07-31** at `docs-drift-and-mypy-fixes`
(branched from `main` @ `e86488d`): **46 errors in 14 files** (checked 92
source files), mypy 2.3.0.

**Correction to the baseline's own bookkeeping.** The earlier baseline's
itemized breakdown (measured 2026-07-27 at `20742d1`) bucketed
`ops/hardening.py:187–318` as "×13" and stated a 46 total, but summing that
table's own rows gives 45, not 46. `orchestrator/ops/hardening.py` has not
been touched by any commit since `20742d1`
(`git log --oneline 20742d1..HEAD -- orchestrator/ops/hardening.py` is
empty), so nothing regressed — the cluster was always **14** errors, not 13.
The "×13" was a miscount from the day it was written, not a defect that
appeared later. Corrected breakdown:

```
orchestrator/ops/hardening.py:187  error: Missing type arguments for generic type "dict"      [type-arg]
orchestrator/ops/hardening.py:187  error: Missing type arguments for generic type "Callable"  [type-arg]
orchestrator/ops/hardening.py:193  error: Missing type arguments for generic type "dict"      [type-arg]
orchestrator/ops/hardening.py:202  error: Argument 3 has incompatible type
    "Callable[[dict[Any, Any]], Coroutine[Any, Any, None]]"; expected
    "Callable[[MutableMapping[str, Any]], Awaitable[None]]"             [arg-type]
orchestrator/ops/hardening.py:219  error: Missing type arguments for generic type "dict"      [type-arg]
orchestrator/ops/hardening.py:219  error: Missing type arguments for generic type "Callable"  [type-arg]
orchestrator/ops/hardening.py:238  error: Missing type arguments for generic type "dict"      [type-arg]
orchestrator/ops/hardening.py:246  error: Returning Any from function declared
    to return "dict[Any, Any]"                                          [no-any-return]
orchestrator/ops/hardening.py:252  error: Missing type arguments for generic type "Callable"  [type-arg]
orchestrator/ops/hardening.py:279  error: Missing type arguments for generic type "dict"      [type-arg]
orchestrator/ops/hardening.py:279  error: Missing type arguments for generic type "Callable"  [type-arg]
orchestrator/ops/hardening.py:293  error: Missing type arguments for generic type "dict"      [type-arg]
orchestrator/ops/hardening.py:305  error: Argument 3 has incompatible type
    "Callable[[dict[Any, Any]], Coroutine[Any, Any, None]]"; expected
    "Callable[[MutableMapping[str, Any]], Awaitable[None]]"             [arg-type]
orchestrator/ops/hardening.py:318  error: Missing type arguments for generic type "dict"      [type-arg]
```

Fourteen rows, all `[type-arg]` (bare `dict`/`Callable` generics on ASGI
middleware signatures) except the two `[arg-type]` rows at `:202`/`:305`
(the same middleware-adapter signature mismatch, twice) and the one
`[no-any-return]` at `:246`. All fourteen are one shape of fix: type the
ASGI `Callable`/`dict` generics precisely enough that `MutableMapping[str,
Any]` / `Awaitable[None]` line up with what Starlette's middleware protocol
actually expects.

**What was found and fixed en route (not part of OP-008's remaining scope):**
`orchestrator/provider/claude.py:145` carried
`# type: ignore[import-not-found]` on `import anthropic`. Now that
`anthropic` is installed in `.venv` (it has been since before this
session — not one of `requirements.txt`'s pins, but present), mypy no longer
raises `import-not-found` there, so the ignore comment itself became a
`[unused-ignore]` error under `--strict`'s `warn_unused_ignores`. Removed;
`# noqa: F401` stays, since ruff's unused-import check is unrelated and the
import is still deliberately unused (its only job is the presence check in
`except ImportError`). This is what brought the baseline from 47/15 back to
**46/14** rather than fixing anything net-new.

**The other 45 errors are unchanged from the 2026-07-27 measurement** (two
line numbers drifted from unrelated code growth above them —
`core/projection.py:383→410`, `workflow/approval.py:458→474` — same
category, not new). Full per-file breakdown is produced by `mypy orchestrator/`.

---

## TD-002 — `fastapi.testclient` / `httpx` deprecation warning

Surfaced by `pytest -q` on 2026-07-31 (2804 passed / 9 skipped, otherwise
clean):

```
.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning:
Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
```

Not a defect — the suite is green — but it is Starlette signaling that the
`TestClient` transport `tests/api/` and `tests/e2e/` depend on will need
`httpx2` at some point. `requirements.txt` pins `httpx==0.28.1` for both
`fastapi.testclient` and any direct HTTP use; no code changes this session.
Worth a scoped look before the next `httpx`/`starlette` bump, not before.
