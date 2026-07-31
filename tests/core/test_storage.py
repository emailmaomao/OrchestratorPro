"""Tests for the SQLite storage layer: schema, migrations, transactions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from orchestrator.core.storage import (
    SCHEMA_VERSION,
    Database,
    SchemaVersionError,
    StorageError,
)


@pytest.fixture
def db() -> Iterator[Database]:
    """A migrated in-memory database."""
    database = Database.in_memory()
    database.migrate()
    yield database
    database.close()


class TestMigration:
    """Schema creation is versioned, idempotent, and refuses to downgrade."""

    def test_fresh_database_starts_unversioned(self) -> None:
        with Database.in_memory() as database:
            assert database.schema_version == 0

    def test_migrate_brings_the_schema_up_to_date(self) -> None:
        with Database.in_memory() as database:
            assert database.migrate() == SCHEMA_VERSION
            assert database.schema_version == SCHEMA_VERSION

    def test_migrate_is_idempotent(self, db: Database) -> None:
        assert db.migrate() == SCHEMA_VERSION
        assert db.migrate() == SCHEMA_VERSION

    def test_every_specified_table_exists(self, db: Database) -> None:
        rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = {row["name"] for row in rows}
        assert {
            "runs",
            "tasks",
            "attempts",
            "events",
            "gate_results",
            "approvals",
        } <= names

    def test_a_newer_database_is_refused(self, db: Database) -> None:
        with db.transaction() as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        with pytest.raises(SchemaVersionError, match="upgrade OrchestratorPro"):
            db.migrate()

    def test_schema_survives_reopening_a_file(self, tmp_dir: Path) -> None:
        path = tmp_dir / "state.db"
        with Database(path) as first:
            first.migrate()
        with Database(path) as second:
            assert second.schema_version == SCHEMA_VERSION
            assert second.migrate() == SCHEMA_VERSION


class TestAppendOnlyEnforcement:
    """The audit log is protected by the engine, not by convention."""

    def _insert(self, db: Database) -> None:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO events (id, run_id, ts, type, payload_json) "
                "VALUES ('evt_1', 'run_1', '2026-01-01T00:00:00+00:00', 'run.created', '{}')"
            )

    def test_update_on_events_is_rejected(self, db: Database) -> None:
        self._insert(db)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with db.transaction() as conn:
                conn.execute("UPDATE events SET type = 'run.started' WHERE id = 'evt_1'")

    def test_delete_on_events_is_rejected(self, db: Database) -> None:
        self._insert(db)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with db.transaction() as conn:
                conn.execute("DELETE FROM events WHERE id = 'evt_1'")

    def test_the_row_survives_a_rejected_mutation(self, db: Database) -> None:
        self._insert(db)
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute("DELETE FROM events")
        assert len(db.query("SELECT id FROM events")) == 1


class TestTransactions:
    """Writes are atomic, and roll back on any exception."""

    def test_committed_work_is_visible(self, db: Database) -> None:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (id, goal, repo_path, status, created_at) "
                "VALUES ('run_a', 'g', '/tmp', 'created', '2026-01-01T00:00:00+00:00')"
            )
        assert db.query_one("SELECT id FROM runs WHERE id = 'run_a'") is not None

    def test_an_exception_rolls_the_whole_block_back(self, db: Database) -> None:
        with pytest.raises(RuntimeError):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO runs (id, goal, repo_path, status, created_at) "
                    "VALUES ('run_b', 'g', '/tmp', 'created', '2026-01-01T00:00:00+00:00')"
                )
                raise RuntimeError("boom")
        assert db.query_one("SELECT id FROM runs WHERE id = 'run_b'") is None

    def test_keyboard_interrupt_also_rolls_back(self, db: Database) -> None:
        """BaseException, not Exception: a Ctrl-C must not leave a partial write."""
        with pytest.raises(KeyboardInterrupt):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO runs (id, goal, repo_path, status, created_at) "
                    "VALUES ('run_c', 'g', '/tmp', 'created', '2026-01-01T00:00:00+00:00')"
                )
                raise KeyboardInterrupt
        assert db.query_one("SELECT id FROM runs WHERE id = 'run_c'") is None

    def test_foreign_keys_are_enforced(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO tasks (id, run_id, title, state) "
                    "VALUES ('task_x', 'run_missing', 't', 'pending')"
                )


class TestQueries:
    """The read helpers behave predictably on empty and populated tables."""

    def test_query_returns_every_row(self, db: Database) -> None:
        assert db.query("SELECT id FROM runs") == []

    def test_query_one_returns_none_when_empty(self, db: Database) -> None:
        assert db.query_one("SELECT id FROM runs WHERE id = 'nope'") is None

    def test_a_bad_query_raises_storage_error(self, db: Database) -> None:
        with pytest.raises(StorageError, match="query failed"):
            db.query("SELECT * FROM table_that_does_not_exist")

    def test_close_is_idempotent(self) -> None:
        database = Database.in_memory()
        database.close()
        database.close()


class TestDurabilitySettings:
    """Crash-safety settings are applied, not merely intended."""

    def test_file_backed_databases_use_wal(self, tmp_dir: Path) -> None:
        with Database(tmp_dir / "state.db") as database:
            database.migrate()
            mode = database.query_one("PRAGMA journal_mode")
            assert mode is not None
            assert str(mode[0]).lower() == "wal"

    def test_file_backed_databases_are_fully_synchronous(self, tmp_dir: Path) -> None:
        with Database(tmp_dir / "state.db") as database:
            row = database.query_one("PRAGMA synchronous")
            assert row is not None
            assert int(row[0]) == 2  # 2 == FULL

    def test_foreign_keys_pragma_is_on(self, db: Database) -> None:
        row = db.query_one("PRAGMA foreign_keys")
        assert row is not None
        assert int(row[0]) == 1
