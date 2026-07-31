"""Durable SQLite storage: schema, migrations, and transactions.

SQLite is chosen for crash-safety and zero operational burden, not throughput
(``ORCHESTRATOR_PRO_SPEC`` §5). The workload is dozens of writes per second at
most, and the property that matters is that a ``SIGKILL`` at any instant leaves
a database that opens cleanly and tells the truth (NFR-2.1).

Three settings carry that guarantee:

* **WAL journaling** — a crash mid-transaction rolls back cleanly, and readers
  never block on the writer.
* **``synchronous=FULL``** — the write-ahead log is flushed before a commit is
  acknowledged. Slower than ``NORMAL``, and the difference is precisely the
  window in which a power loss could lose an acknowledged commit.
* **Append-only triggers on ``events``** — the audit log is protected by the
  database itself, not by the discipline of callers. ``UPDATE`` and ``DELETE``
  on that table abort at the engine level.

Implemented against the standard library's :mod:`sqlite3` rather than an ORM.
``CLAUDE.md`` names SQLAlchemy; it is not installed, and the schema here is six
flat tables with no relational mapping worth the dependency. See ``TASKS.md``
for the open decision.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from orchestrator.core.events import OrchestratorError

__all__ = [
    "SCHEMA_VERSION",
    "Database",
    "SchemaVersionError",
    "StorageError",
]

#: Bumped whenever the DDL below changes. A database written by a newer version
#: is refused rather than silently misread.
SCHEMA_VERSION: Final = 1

_MEMORY: Final = ":memory:"


class StorageError(OrchestratorError):
    """A storage operation failed."""

    code = "storage"
    retryable = False


class SchemaVersionError(StorageError):
    """The database was written by an incompatible version of the schema."""

    code = "schema_version"
    retryable = False


_DDL_V1: Final = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    goal         TEXT NOT NULL,
    repo_path    TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    finished_at  TEXT,
    config_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    prompt          TEXT NOT NULL DEFAULT '',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    state           TEXT NOT NULL,
    max_attempts    INTEGER NOT NULL DEFAULT 1,
    attempts_made   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);

CREATE TABLE IF NOT EXISTS attempts (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id         TEXT,
    n              INTEGER NOT NULL,
    adapter        TEXT,
    workspace_path TEXT,
    branch         TEXT,
    status         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id);

CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    run_id       TEXT,
    task_id      TEXT,
    attempt_id   TEXT,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);

-- The audit log is append-only, enforced by the engine rather than by the
-- discipline of callers. A bug that tries to rewrite history aborts here.
CREATE TRIGGER IF NOT EXISTS events_forbid_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS events_forbid_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS gate_results (
    id          TEXT PRIMARY KEY,
    attempt_id  TEXT NOT NULL,
    run_id      TEXT,
    gate        TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_gate_attempt ON gate_results(attempt_id);
CREATE INDEX IF NOT EXISTS idx_gate_run ON gate_results(run_id);

CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    run_id       TEXT,
    requested_at TEXT NOT NULL,
    resolved_at  TEXT,
    decision     TEXT,
    actor        TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id);
"""

#: Ordered migrations. Index *i* upgrades the schema from version *i* to *i+1*.
_MIGRATIONS: Final[tuple[str, ...]] = (_DDL_V1,)


class Database:
    """A connection to one OrchestratorPro state database.

    One instance owns one connection. Access is serialized by an internal lock,
    so the object is safe to share across threads, though the intended use is a
    single asyncio loop calling through :func:`asyncio.to_thread`.

    Transactions are explicit: :meth:`transaction` issues ``BEGIN IMMEDIATE``,
    so a writer takes its lock up front rather than discovering a conflict at
    commit time. Autocommit is disabled — every write goes through a
    transaction, which is what lets an event and the tables it updates land
    atomically (``ORCHESTRATOR_PRO_SPEC`` §5).
    """

    __slots__ = ("_conn", "_lock", "_path")

    def __init__(self, path: Path | str, *, timeout: float = 30.0) -> None:
        """Open (and create, if needed) a database at ``path``.

        Args:
            path: Filesystem path, or ``":memory:"`` for an ephemeral database.
            timeout: Seconds to wait for a competing writer's lock.

        Raises:
            StorageError: If the database cannot be opened.
        """
        self._path = str(path)
        self._lock = threading.RLock()
        try:
            # isolation_level=None disables the driver's implicit transaction
            # handling so that BEGIN/COMMIT below are the only ones issued.
            self._conn = sqlite3.connect(
                self._path,
                timeout=timeout,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise StorageError(
                f"could not open database at {self._path}: {exc}",
                detail={"path": self._path},
            ) from exc

        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()

    @classmethod
    def in_memory(cls) -> Database:
        """Open an ephemeral database. Intended for tests."""
        return cls(_MEMORY)

    @property
    def path(self) -> str:
        """The filesystem path, or ``":memory:"``."""
        return self._path

    @property
    def is_memory(self) -> bool:
        """Whether this database lives only in memory."""
        return self._path == _MEMORY

    def _apply_pragmas(self) -> None:
        """Configure durability and integrity settings for the connection."""
        conn = self._conn
        conn.execute("PRAGMA foreign_keys = ON")
        if not self.is_memory:
            # WAL is unavailable for in-memory databases, where durability is
            # moot in any case.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")

    @property
    def schema_version(self) -> int:
        """The schema version recorded in the file, or ``0`` if uninitialized."""
        with self._lock:
            row = self._conn.execute("PRAGMA user_version").fetchone()
            return int(row[0])

    def migrate(self) -> int:
        """Bring the schema up to :data:`SCHEMA_VERSION`.

        Idempotent: running it against an up-to-date database does nothing.

        Returns:
            The schema version after migrating.

        Raises:
            SchemaVersionError: If the database is newer than this code.
            StorageError: If a migration statement fails.
        """
        with self._lock:
            current = self.schema_version
            if current > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database at {self._path} is schema version {current}, but this "
                    f"build understands at most {SCHEMA_VERSION}; upgrade "
                    "OrchestratorPro rather than downgrading the database",
                    detail={"found": current, "supported": SCHEMA_VERSION},
                )
            if current == SCHEMA_VERSION:
                return current

            try:
                for version in range(current, SCHEMA_VERSION):
                    # executescript() issues its own COMMIT, so it cannot run
                    # inside transaction(); the wrapper would then find no open
                    # transaction to close. Atomicity is not lost that matters
                    # here: every statement is IF NOT EXISTS, so a migration
                    # interrupted partway is simply re-applied on the next call,
                    # and user_version only advances once the script completed.
                    self._conn.executescript(_MIGRATIONS[version])
                    # PRAGMA cannot be parameterized, and the value is an int
                    # from a range() over our own constant — not caller input.
                    self._conn.execute(f"PRAGMA user_version = {version + 1}")
            except sqlite3.Error as exc:
                raise StorageError(
                    f"schema migration failed: {exc}", detail={"path": self._path}
                ) from exc
            return self.schema_version

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one atomic transaction.

        Commits on clean exit, rolls back on any exception — including
        :class:`KeyboardInterrupt` and :class:`SystemExit`, which is why the
        handler catches :class:`BaseException`. A partial write surviving a
        Ctrl-C would violate the crash-safety guarantee just as surely as one
        surviving a segfault.

        Yields:
            The live connection, for the duration of the transaction.

        Raises:
            StorageError: If the transaction cannot be started or committed.
        """
        with self._lock:
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StorageError(f"could not begin transaction: {exc}") from exc
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                try:
                    conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    conn.execute("ROLLBACK")
                    raise StorageError(f"could not commit transaction: {exc}") from exc

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Run a read-only query and return every row.

        Args:
            sql: A ``SELECT`` statement.
            params: Bound parameters.

        Returns:
            The result rows.

        Raises:
            StorageError: If the query fails.
        """
        with self._lock:
            try:
                return list(self._conn.execute(sql, params))
            except sqlite3.Error as exc:
                raise StorageError(f"query failed: {exc}", detail={"sql": sql}) from exc

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Run a read-only query and return the first row, or ``None``."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        """Close the connection. Idempotent."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - close should not fail
                pass

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
