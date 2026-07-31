"""Retention: archiving finished runs, pruning what is left, and proving it.

The event log is append-only, enforced by triggers in the database rather than
by the discipline of callers. That is exactly right for application code and
exactly wrong for a retention policy, which must eventually delete something.

So retention is a **deliberate administrative operation**, and it says so:

1. A run is exported to a compressed archive with a digest over its events.
2. The archive is verified — read back, digested, and replayed into the same
   state the live database holds.
3. Only then is the run removed, inside one transaction that drops the
   append-only triggers and puts them back.

Step 2 is the one that matters. Archive-then-delete without verifying is how an
organisation discovers, months later, that its backups were empty. Nothing here
deletes anything it has not just successfully read back.

Worktrees and transcripts are **not** covered. Worktrees belong to
``git_manager`` and a retention policy that deletes a directory an operator is
mid-way through inspecting is worse than one that leaves it; that remains open
(spec §13, Q5).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orchestrator.core.events import Event, OrchestratorError, RunId
from orchestrator.core.logging import get_logger
from orchestrator.core.records import RunStatus
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database

__all__ = [
    "ARCHIVE_SUFFIX",
    "ArchiveManifest",
    "archive_path",
    "list_archives",
    "read_archive",
    "verify_all",
    "RetentionError",
    "RetentionPlan",
    "RetentionPolicy",
    "RetentionReport",
    "apply_retention",
    "archive_run",
    "plan_retention",
    "prune_run",
    "restore_run",
    "verify_archive",
]

_log = get_logger(__name__)

#: Written for each archived run.
ARCHIVE_SUFFIX = ".run.json.gz"

#: The triggers retention must step around, and put back.
_APPEND_ONLY_TRIGGERS = ("events_forbid_update", "events_forbid_delete")


class RetentionError(OrchestratorError):
    """A retention operation could not be completed."""

    code = "retention"
    retryable = False


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """What to keep.

    Attributes:
        keep_runs: How many finished runs to keep in the live database,
            newest first. ``0`` keeps none; ``None`` keeps all.
        keep_days: Keep any run finished within this many days regardless of
            the count. Both conditions must permit removal, so a busy week does
            not evict something from this morning.
        archive: Whether to write an archive before removing anything. Off
            means delete, and the field is named so nobody can set it by
            accident.
        keep_backups: How many database snapshots to retain.
        keep_sessions: Whether to prune expired and revoked login sessions.
        include_cancelled: Whether a cancelled run is eligible. Off by default:
            a cancelled run is usually one somebody means to resume.
    """

    keep_runs: int | None = 100
    keep_days: int = 30
    archive: bool = True
    keep_backups: int = 7
    keep_sessions: bool = True
    include_cancelled: bool = False

    def __post_init__(self) -> None:
        if self.keep_runs is not None and self.keep_runs < 0:
            raise RetentionError(
                f"keep_runs must not be negative, got {self.keep_runs}"
            )
        if self.keep_days < 0:
            raise RetentionError(f"keep_days must not be negative, got {self.keep_days}")
        if self.keep_backups < 1:
            raise RetentionError(
                "keep_backups must be at least 1; a retention policy that can "
                "empty the backup directory is a deletion policy"
            )

    @property
    def keeps_everything(self) -> bool:
        """Whether this policy would remove nothing, ever."""
        return self.keep_runs is None

    def describe(self) -> str:
        """A sentence an operator can check against what they meant."""
        if self.keeps_everything:
            return "keep every run"
        return (
            f"keep the newest {self.keep_runs} finished run(s) and anything "
            f"from the last {self.keep_days} day(s); "
            f"{'archive' if self.archive else 'DELETE WITHOUT ARCHIVING'} the rest"
        )


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """What an archive holds, and how to tell it is intact."""

    run_id: str
    goal: str
    status: str
    events: int
    tasks: int
    created_at: str
    digest: str
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Render for storage."""
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "events": self.events,
            "tasks": self.tasks,
            "created_at": self.created_at,
            "digest": self.digest,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ArchiveManifest:
        """Read a manifest.

        Raises:
            RetentionError: If it is not readable as one.
        """
        try:
            return cls(
                run_id=str(data["run_id"]),
                goal=str(data["goal"]),
                status=str(data["status"]),
                events=int(data["events"]),
                tasks=int(data["tasks"]),
                created_at=str(data["created_at"]),
                digest=str(data["digest"]),
                schema_version=int(data.get("schema_version", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RetentionError(f"the archive manifest is not readable: {exc}") from exc

    def summary(self) -> str:
        """A one-line account."""
        return (
            f"{self.run_id}: {self.events} event(s), {self.tasks} task(s), "
            f"{self.status}, archived {self.created_at}"
        )


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """What a policy would do, before it does it."""

    eligible: tuple[RunId, ...] = ()
    kept: tuple[RunId, ...] = ()
    reason: Mapping = field(default_factory=dict)  # type: ignore[type-arg]

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to do."""
        return not self.eligible

    def summary(self) -> str:
        """A one-line account."""
        if self.is_empty:
            return f"nothing to remove; {len(self.kept)} run(s) kept"
        return f"{len(self.eligible)} run(s) eligible, {len(self.kept)} kept"


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """What a retention pass actually did."""

    archived: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()
    backups_removed: tuple[str, ...] = ()
    sessions_pruned: int = 0
    bytes_written: int = 0
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether everything the pass attempted succeeded."""
        return not self.failures

    def summary(self) -> str:
        """A one-line account."""
        parts = [
            f"{len(self.archived)} archived",
            f"{len(self.pruned)} pruned",
            f"{len(self.backups_removed)} backup(s) removed",
        ]
        if self.sessions_pruned:
            parts.append(f"{self.sessions_pruned} session(s) pruned")
        if self.failures:
            parts.append(f"{len(self.failures)} FAILED")
        return ", ".join(parts)


def _digest_events(events: Sequence[Event]) -> str:
    """Digest a run's events, in order."""
    digest = hashlib.sha256()
    for event in events:
        digest.update(event.to_json().encode("utf-8"))
    return digest.hexdigest()


def archive_path(directory: Path, run_id: RunId) -> Path:
    """Where a run's archive lives."""
    return directory / f"{run_id}{ARCHIVE_SUFFIX}"


def archive_run(store: RunStore, run_id: RunId, directory: Path) -> ArchiveManifest:
    """Write a run's complete log to a compressed archive.

    Args:
        store: The run store.
        run_id: The run to archive.
        directory: Where to write. Created if absent.

    Returns:
        The manifest.

    Raises:
        RetentionError: If the run has no events, or the archive already exists.
    """
    events = store.events.read_run(run_id)
    if not events:
        raise RetentionError(
            f"run {run_id} has no events to archive", detail={"run_id": str(run_id)}
        )

    directory.mkdir(parents=True, exist_ok=True)
    target = archive_path(directory, run_id)
    if target.exists():
        raise RetentionError(
            f"{target.name} already exists; refusing to overwrite an archive",
            detail={"path": str(target)},
        )

    state = store.replay(run_id)
    manifest = ArchiveManifest(
        run_id=str(run_id),
        goal=state.goal,
        status=state.status.value,
        events=len(events),
        tasks=len(state.tasks),
        created_at=datetime.now(UTC).isoformat(),
        digest=_digest_events(events),
    )
    payload = {
        "manifest": manifest.to_dict(),
        "events": [json.loads(event.to_json()) for event in events],
    }

    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)

    _log.info("run archived", run_id=str(run_id), events=len(events), path=str(target))
    return manifest


def read_archive(path: Path) -> tuple[ArchiveManifest, tuple[Event, ...]]:
    """Read an archive back.

    Raises:
        RetentionError: If it cannot be read or is not an archive.
    """
    if not path.is_file():
        raise RetentionError(f"there is no archive at {path}")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, EOFError) as exc:
        raise RetentionError(
            f"{path.name} could not be read: {exc}", detail={"path": str(path)}
        ) from exc

    if not isinstance(payload, dict) or "manifest" not in payload:
        raise RetentionError(f"{path.name} is not a run archive")

    manifest = ArchiveManifest.from_dict(payload["manifest"])
    try:
        events = tuple(Event.from_json(json.dumps(entry)) for entry in payload["events"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RetentionError(
            f"{path.name} holds events this build cannot read: {exc}"
        ) from exc
    return manifest, events


def verify_archive(path: Path) -> ArchiveManifest:
    """Check that an archive is intact and replays.

    Both halves matter. The digest proves the bytes survived; the replay proves
    they still mean something — an archive that decompresses but no longer
    reconstructs a run is not a backup of anything.

    Raises:
        RetentionError: If the archive is damaged or does not replay.
    """
    from orchestrator.core.projection import reconstruct

    manifest, events = read_archive(path)

    actual = _digest_events(events)
    if actual != manifest.digest:
        raise RetentionError(
            f"{path.name} does not match its manifest; it has been altered",
            detail={"expected": manifest.digest, "actual": actual},
        )

    state = reconstruct(events, run_id=RunId(manifest.run_id))
    if state.event_count != manifest.events or len(state.tasks) != manifest.tasks:
        raise RetentionError(
            f"{path.name} does not replay into what its manifest describes",
            detail={
                "manifest": [manifest.events, manifest.tasks],
                "replayed": [state.event_count, len(state.tasks)],
            },
        )
    return manifest


def prune_run(store: RunStore, run_id: RunId) -> int:
    """Remove a run and everything belonging to it from the live database.

    The append-only triggers are dropped and restored inside the same
    transaction. That is not a loophole: retention is the one operation
    permitted to remove history, and it is deliberately the only code that
    touches those triggers.

    Returns:
        How many events were removed.

    Raises:
        RetentionError: If the run does not exist.
    """
    events = store.events.count(run_id)
    if not events:
        raise RetentionError(
            f"run {run_id} is not in the live database", detail={"run_id": str(run_id)}
        )

    with store.db.transaction() as conn:
        for trigger in _APPEND_ONLY_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        try:
            conn.execute("DELETE FROM gate_results WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM approvals WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM attempts WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM tasks WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM events WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM runs WHERE id = ?", (str(run_id),))
        finally:
            # In the same transaction, so a failure between the drop and the
            # restore rolls back to a database that is still protected.
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS events_forbid_update "
                "BEFORE UPDATE ON events BEGIN "
                "SELECT RAISE(ABORT, 'events is append-only: UPDATE is forbidden'); END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS events_forbid_delete "
                "BEFORE DELETE ON events BEGIN "
                "SELECT RAISE(ABORT, 'events is append-only: DELETE is forbidden'); END"
            )

    _log.warning("run pruned from the live database", run_id=str(run_id), events=events)
    return events


def restore_run(store: RunStore, path: Path) -> int:
    """Load an archived run back into a database.

    Verified first, so a damaged archive cannot be restored over anything.

    Returns:
        How many events were restored.

    Raises:
        RetentionError: If the archive is damaged or the run is already present.
    """
    manifest = verify_archive(path)
    run_id = RunId(manifest.run_id)

    if store.events.count(run_id):
        raise RetentionError(
            f"run {run_id} is already in this database",
            detail={"run_id": manifest.run_id},
        )

    _, events = read_archive(path)
    restored = store.record_all(events)
    _log.info("run restored from archive", run_id=manifest.run_id, events=restored)
    return restored


def plan_retention(
    store: RunStore, policy: RetentionPolicy, *, now: datetime | None = None
) -> RetentionPlan:
    """Work out which runs a policy would remove.

    A run is eligible only when it is finished, older than ``keep_days``, and
    outside the newest ``keep_runs``. All three, because any one of them alone
    has an obvious failure: a busy week evicts this morning's run, a quiet month
    evicts nothing, and an active run gets archived mid-flight.
    """
    if policy.keeps_everything:
        return RetentionPlan(kept=tuple(store.run_ids()))

    when = now or datetime.now(UTC)
    cutoff = when - timedelta(days=policy.keep_days)

    terminal = {RunStatus.FINISHED}
    if policy.include_cancelled:
        terminal.add(RunStatus.CANCELLED)

    finished: list[tuple[str, RunId]] = []
    kept: list[RunId] = []

    for run_id in store.run_ids():
        state = store.replay(run_id)
        if state.status not in terminal:
            kept.append(run_id)
            continue
        stamp = state.finished_at or state.created_at
        if stamp is None or stamp > cutoff:
            kept.append(run_id)
            continue
        finished.append((stamp.isoformat(), run_id))

    finished.sort(reverse=True)
    keep_newest = policy.keep_runs or 0
    kept.extend(run_id for _, run_id in finished[:keep_newest])
    eligible = tuple(run_id for _, run_id in finished[keep_newest:])

    return RetentionPlan(eligible=eligible, kept=tuple(kept))


def apply_retention(
    store: RunStore,
    policy: RetentionPolicy,
    *,
    directory: Path,
    dry_run: bool = True,
    auth_store: Any = None,
    backup_directory: Path | None = None,
    now: datetime | None = None,
) -> RetentionReport:
    """Run a retention pass.

    Args:
        store: The run store.
        policy: What to keep.
        directory: Where archives are written.
        dry_run: Report what would happen without doing it. **On by default**:
            a retention command whose default is destructive is one that gets
            run by accident exactly once.
        auth_store: Prunes expired sessions when supplied.
        backup_directory: Prunes old snapshots when supplied.
        now: Injected for tests.

    Returns:
        What was done, or would have been.
    """
    plan = plan_retention(store, policy, now=now)
    if dry_run:
        return RetentionReport(
            archived=tuple(str(run_id) for run_id in plan.eligible) if policy.archive else (),
            pruned=tuple(str(run_id) for run_id in plan.eligible),
        )

    archived: list[str] = []
    pruned: list[str] = []
    failures: list[str] = []
    written = 0

    for run_id in plan.eligible:
        try:
            if policy.archive:
                manifest = archive_run(store, run_id, directory)
                # Verified before anything is removed. Archive-then-delete
                # without this is how a backup turns out to be empty.
                verify_archive(archive_path(directory, run_id))
                archived.append(manifest.run_id)
                written += archive_path(directory, run_id).stat().st_size
            prune_run(store, run_id)
            pruned.append(str(run_id))
        except (RetentionError, OSError) as exc:
            failures.append(f"{run_id}: {exc}")
            _log.error("retention failed for a run", run_id=str(run_id), error=str(exc))

    backups_removed: tuple[str, ...] = ()
    if backup_directory is not None:
        from orchestrator.ops.backup import prune_backups

        backups_removed = tuple(
            str(path) for path in prune_backups(backup_directory, keep=policy.keep_backups)
        )

    sessions = 0
    if auth_store is not None and policy.keep_sessions:
        sessions = auth_store.prune_sessions()

    return RetentionReport(
        archived=tuple(archived),
        pruned=tuple(pruned),
        backups_removed=backups_removed,
        sessions_pruned=sessions,
        bytes_written=written,
        failures=tuple(failures),
    )


def list_archives(directory: Path) -> tuple[ArchiveManifest, ...]:
    """Every archive in a directory, newest first.

    An unreadable file is skipped rather than raising: one damaged archive
    should not stop an operator from seeing the others.
    """
    if not directory.is_dir():
        return ()

    found: list[ArchiveManifest] = []
    for path in sorted(directory.glob(f"*{ARCHIVE_SUFFIX}")):
        try:
            manifest, _ = read_archive(path)
        except RetentionError:
            _log.warning("unreadable archive", path=str(path))
            continue
        found.append(manifest)
    return tuple(sorted(found, key=lambda m: m.created_at, reverse=True))


def verify_all(directory: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Verify every archive in a directory.

    Returns:
        The archives that are intact, and the ones that are not.
    """
    good: list[str] = []
    bad: list[str] = []
    for path in sorted(Path(directory).glob(f"*{ARCHIVE_SUFFIX}")):
        try:
            verify_archive(path)
            good.append(path.name)
        except RetentionError as exc:
            bad.append(f"{path.name}: {exc}")
    return tuple(good), tuple(bad)

