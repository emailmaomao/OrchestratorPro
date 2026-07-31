"""Backup and restore of the run database.

The event log is the system's memory: lose it and every run becomes
unrecoverable, whatever is still on disk in a worktree. So the backup path is
held to the same standard as the write path.

Three decisions:

* **Online backup, not file copy.** SQLite's backup API takes a consistent
  snapshot of a database that is being written to. Copying the file while a
  transaction is in flight — or copying it without its write-ahead log — yields
  something that opens cleanly and is missing the last few minutes.
* **Every backup carries a manifest.** Size, digest, schema version, and how
  many events it holds. A backup nobody can verify is a backup nobody should
  rely on, and "it exists" is not verification.
* **Restore refuses to overwrite silently.** The existing database is moved
  aside first. An operator restoring the wrong snapshot at four in the morning
  should be able to undo it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.core.events import OrchestratorError
from orchestrator.core.logging import get_logger
from orchestrator.core.storage import SCHEMA_VERSION, Database

__all__ = [
    "MANIFEST_SUFFIX",
    "BackupError",
    "BackupManifest",
    "create_backup",
    "list_backups",
    "prune_backups",
    "restore_backup",
    "verify_backup",
]

_log = get_logger(__name__)

#: Written beside each snapshot.
MANIFEST_SUFFIX = ".manifest.json"

#: Read in chunks so a large database does not have to fit in memory twice.
_DIGEST_CHUNK = 1 << 20


class BackupError(OrchestratorError):
    """A backup could not be taken, verified, or restored."""

    code = "backup"
    retryable = False


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """What a snapshot contains, and how to tell it is intact."""

    path: str
    created_at: str
    size_bytes: int
    digest: str
    schema_version: int
    runs: int
    events: int
    source: str = ""

    def to_json(self) -> str:
        """Render deterministically, so two identical manifests compare equal."""
        return json.dumps(
            {
                "path": self.path,
                "created_at": self.created_at,
                "size_bytes": self.size_bytes,
                "digest": self.digest,
                "schema_version": self.schema_version,
                "runs": self.runs,
                "events": self.events,
                "source": self.source,
            },
            sort_keys=True,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> BackupManifest:
        """Parse a manifest.

        Raises:
            BackupError: If it is not readable as one.
        """
        try:
            data = json.loads(text)
            return cls(
                path=str(data["path"]),
                created_at=str(data["created_at"]),
                size_bytes=int(data["size_bytes"]),
                digest=str(data["digest"]),
                schema_version=int(data["schema_version"]),
                runs=int(data["runs"]),
                events=int(data["events"]),
                source=str(data.get("source", "")),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise BackupError(f"the manifest is not readable: {exc}") from exc

    def summary(self) -> str:
        """A one-line account."""
        return (
            f"{Path(self.path).name}: {self.events} event(s) across {self.runs} run(s), "
            f"{self.size_bytes / 1_048_576:.1f} MiB, taken {self.created_at}"
        )


def digest_of(path: Path) -> str:
    """Return the SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(path: Path) -> tuple[int, int, int]:
    """Return the schema version and the run and event counts of a database."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        runs = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        events = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return version, runs, events
    except sqlite3.DatabaseError as exc:
        raise BackupError(
            f"{path} is not a readable OrchestratorPro database: {exc}",
            detail={"path": str(path)},
        ) from exc
    finally:
        connection.close()


def create_backup(
    database: Path | Database,
    destination: Path,
    *,
    label: str = "",
    now: datetime | None = None,
) -> BackupManifest:
    """Take a consistent snapshot of a live database.

    Args:
        database: The database, or a path to it.
        destination: Directory to write into. Created if absent.
        label: Appended to the filename, for naming a snapshot after why it was
            taken — ``pre-upgrade``, say.
        now: Timestamp, injected so a test can name a file predictably.

    Returns:
        The manifest, which is also written beside the snapshot.

    Raises:
        BackupError: If the source is missing or the snapshot cannot be taken.
    """
    source = Path(database.path) if isinstance(database, Database) else Path(database)
    if str(source) == ":memory:":
        raise BackupError(
            "an in-memory database cannot be backed up; it has no file to copy",
            detail={"path": str(source)},
        )
    if not source.is_file():
        raise BackupError(
            f"there is no database at {source}", detail={"path": str(source)}
        )

    destination.mkdir(parents=True, exist_ok=True)
    # Milliseconds, not seconds: two snapshots a moment apart is an ordinary
    # thing to do — before and after an upgrade, say — and a name collision
    # would refuse the second one for no reason a person would recognize.
    taken = now or datetime.now(UTC)
    stamp = f"{taken.strftime('%Y%m%dT%H%M%S')}{taken.microsecond // 1000:03d}Z"
    name = f"orchestrator-{stamp}{'-' + label if label else ''}.db"
    target = destination / name

    if target.exists():
        raise BackupError(
            f"{target} already exists; refusing to overwrite a snapshot",
            detail={"path": str(target)},
        )

    # The backup API copies a consistent view even while the source is being
    # written. Copying the file would race the write-ahead log.
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise BackupError(
            f"the snapshot could not be taken: {exc}", detail={"path": str(source)}
        ) from exc
    finally:
        copy.close()
        origin.close()

    version, runs, events = _counts(target)
    manifest = BackupManifest(
        path=str(target),
        created_at=taken.isoformat(),
        size_bytes=target.stat().st_size,
        digest=digest_of(target),
        schema_version=version,
        runs=runs,
        events=events,
        source=str(source),
    )
    manifest_path(target).write_text(manifest.to_json(), encoding="utf-8")

    _log.info(
        "backup taken", path=str(target), events=events, runs=runs, bytes=manifest.size_bytes
    )
    return manifest


def manifest_path(snapshot: Path) -> Path:
    """Return the manifest path for a snapshot."""
    return snapshot.with_name(snapshot.name + MANIFEST_SUFFIX)


def verify_backup(snapshot: Path) -> BackupManifest:
    """Check that a snapshot is intact and matches its manifest.

    Args:
        snapshot: The ``.db`` file.

    Returns:
        The manifest.

    Raises:
        BackupError: If the file, the manifest, or the agreement between them
            is missing or wrong.
    """
    if not snapshot.is_file():
        raise BackupError(f"there is no snapshot at {snapshot}")

    manifest_file = manifest_path(snapshot)
    if not manifest_file.is_file():
        raise BackupError(
            f"{snapshot.name} has no manifest, so it cannot be verified",
            detail={"path": str(snapshot)},
        )

    manifest = BackupManifest.from_json(manifest_file.read_text(encoding="utf-8"))
    actual = digest_of(snapshot)
    if actual != manifest.digest:
        raise BackupError(
            f"{snapshot.name} does not match its manifest; it has been altered "
            "or truncated since it was taken",
            detail={"expected": manifest.digest, "actual": actual},
        )

    version, runs, events = _counts(snapshot)
    if (version, runs, events) != (
        manifest.schema_version,
        manifest.runs,
        manifest.events,
    ):
        raise BackupError(
            f"{snapshot.name} does not hold what its manifest describes",
            detail={
                "manifest": [manifest.schema_version, manifest.runs, manifest.events],
                "actual": [version, runs, events],
            },
        )
    return manifest


def restore_backup(
    snapshot: Path, destination: Path, *, keep_existing: bool = True
) -> BackupManifest:
    """Restore a snapshot over a database.

    The snapshot is verified before anything is touched, and any existing
    database is moved aside rather than deleted, so restoring the wrong one is
    recoverable.

    Args:
        snapshot: The ``.db`` file to restore.
        destination: Where the database should end up.
        keep_existing: Whether to preserve what is already there.

    Returns:
        The restored snapshot's manifest.

    Raises:
        BackupError: If verification fails, or the schema is from the future.
    """
    manifest = verify_backup(snapshot)

    if manifest.schema_version > SCHEMA_VERSION:
        raise BackupError(
            f"{snapshot.name} was written by a newer build (schema "
            f"{manifest.schema_version}, this build understands {SCHEMA_VERSION}); "
            "upgrade before restoring",
            detail={"snapshot": manifest.schema_version, "supported": SCHEMA_VERSION},
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if keep_existing:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            aside = destination.with_name(f"{destination.name}.replaced-{stamp}")
            destination.replace(aside)
            _log.warning("existing database moved aside", path=str(aside))
        else:
            destination.unlink()

    shutil.copy2(snapshot, destination)
    # The journal and shared-memory files belong to the database that was there
    # before; leaving them would make SQLite try to recover a log that describes
    # a different file.
    for leftover in (f"{destination}-wal", f"{destination}-shm"):
        Path(leftover).unlink(missing_ok=True)

    _log.info("backup restored", source=str(snapshot), destination=str(destination))
    return manifest


def list_backups(directory: Path) -> tuple[BackupManifest, ...]:
    """Return every snapshot in a directory, newest first.

    A snapshot with no manifest, or one that cannot be parsed, is skipped
    rather than raising: an unreadable file in the backup directory should not
    stop an operator from seeing the others.
    """
    if not directory.is_dir():
        return ()

    found: list[BackupManifest] = []
    for snapshot in sorted(directory.glob("*.db")):
        file = manifest_path(snapshot)
        if not file.is_file():
            continue
        try:
            found.append(BackupManifest.from_json(file.read_text(encoding="utf-8")))
        except BackupError:
            _log.warning("unreadable backup manifest", path=str(file))
    return tuple(sorted(found, key=lambda m: m.created_at, reverse=True))


def prune_backups(
    directory: Path, *, keep: int = 7, dry_run: bool = False
) -> tuple[Path, ...]:
    """Delete all but the newest ``keep`` snapshots.

    Args:
        directory: The backup directory.
        keep: How many to retain. Must be at least one — a retention policy
            that can empty the directory is a deletion policy.
        dry_run: Report what would be deleted without deleting it.

    Returns:
        The snapshots removed, oldest first.

    Raises:
        BackupError: If ``keep`` is less than one.
    """
    if keep < 1:
        raise BackupError(f"keep must be at least 1, got {keep}", detail={"keep": keep})

    doomed = [Path(manifest.path) for manifest in list_backups(directory)[keep:]]
    if dry_run:
        return tuple(reversed(doomed))

    for snapshot in doomed:
        snapshot.unlink(missing_ok=True)
        manifest_path(snapshot).unlink(missing_ok=True)
        _log.info("backup pruned", path=str(snapshot))
    return tuple(reversed(doomed))


def total_size(manifests: Iterable[BackupManifest]) -> int:
    """Return the combined size of a set of snapshots, in bytes."""
    return sum(manifest.size_bytes for manifest in manifests)
