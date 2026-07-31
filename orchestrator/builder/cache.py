"""The build cache and artifact tracking.

A cache entry answers one question: *has this exact unit, from these exact
inputs, already been built successfully?* The key is a fingerprint over
everything that can change the answer — the unit's identity, the digests of its
sources, and the fingerprints of its dependencies. Dependencies are included
transitively by construction, so a change deep in the graph invalidates
everything built on top of it without anyone walking the graph twice.

**A cache hit is verified before it is trusted.** The entry records the
artifacts the build produced; if any of them is missing from disk or its content
no longer matches, the hit is discarded. A cache that says "already built" about
outputs somebody deleted is worse than no cache at all — it turns a rebuild into
a silent failure much later, in whatever consumed the missing file.

Persistence goes through the M2 :class:`~orchestrator.core.storage.Database`,
which already carries the durability guarantees (WAL, ``synchronous=FULL``). The
build tables are owned here rather than added to the core schema: they are this
subsystem's private state, not part of the run record, and a cache that can be
deleted without touching the event log is a cache an operator can safely clear.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from orchestrator.builder.model import (
    Artifact,
    BuildStatus,
    BuildUnit,
    Diagnostic,
    ProjectLayout,
    Severity,
    digest_bytes,
    digest_text,
    stable_json,
)
from orchestrator.core.storage import Database

__all__ = [
    "ArtifactTracker",
    "BuildCache",
    "CacheEntry",
    "MemoryCache",
    "SqliteCache",
    "fingerprint_for",
    "fingerprints_for",
]

_DDL = """
CREATE TABLE IF NOT EXISTS build_cache (
    key           TEXT PRIMARY KEY,
    unit          TEXT NOT NULL,
    status        TEXT NOT NULL,
    command       TEXT NOT NULL DEFAULT '',
    duration_s    REAL NOT NULL DEFAULT 0,
    output        TEXT NOT NULL DEFAULT '',
    artifacts_json   TEXT NOT NULL DEFAULT '[]',
    diagnostics_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_build_cache_unit ON build_cache(unit);

CREATE TABLE IF NOT EXISTS build_artifacts (
    key    TEXT NOT NULL,
    unit   TEXT NOT NULL,
    path   TEXT NOT NULL,
    digest TEXT NOT NULL,
    size   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, path)
);
CREATE INDEX IF NOT EXISTS idx_build_artifacts_unit ON build_artifacts(unit);
"""


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One remembered build."""

    key: str
    unit: str
    status: BuildStatus = BuildStatus.SUCCEEDED
    artifacts: tuple[Artifact, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    command: str = ""
    duration_s: float = 0.0
    output: str = ""

    @property
    def usable(self) -> bool:
        """Whether this entry may stand in for a rebuild.

        Only a success is reusable. Remembering a failure would be useful for
        reporting and disastrous for correctness — the fix for a failed build is
        usually a change the fingerprint does not see, such as an upgraded
        toolchain.
        """
        return self.status is BuildStatus.SUCCEEDED

    def verify(self, root: Path) -> tuple[bool, tuple[str, ...]]:
        """Check the recorded artifacts are still on disk and unchanged.

        Returns:
            Whether the entry is still valid, and the paths that are missing or
            have drifted.
        """
        stale: list[str] = []
        for artifact in self.artifacts:
            path = root / artifact.path
            if not path.is_file():
                stale.append(artifact.path)
                continue
            if digest_bytes(path.read_bytes()) != artifact.digest:
                stale.append(artifact.path)
        return (not stale, tuple(sorted(stale)))


def fingerprint_for(
    unit: BuildUnit,
    layout: ProjectLayout,
    dependency_fingerprints: Mapping[str, str],
) -> str:
    """Compute one unit's cache key.

    Args:
        unit: The unit being fingerprinted.
        layout: Supplies the source digests.
        dependency_fingerprints: The already-computed keys of its dependencies.

    Returns:
        A hex digest.

    Raises:
        KeyError: If a dependency has no fingerprint yet. Fingerprints must be
            computed in dependency order; see :func:`fingerprints_for`.
    """
    sources = {
        path: source.digest
        for path, source in sorted(layout.sources.items())
        if unit.owns(path)
    }
    return digest_text(
        stable_json(
            {
                "unit": unit.identity(),
                "sources": sources,
                "dependencies": {
                    name: dependency_fingerprints[name]
                    for name in sorted(unit.depends_on)
                },
            }
        )
    )


def fingerprints_for(
    layout: ProjectLayout, order: Sequence[str]
) -> dict[str, str]:
    """Fingerprint every unit, in dependency order.

    Args:
        layout: The project.
        order: Unit names, topologically sorted.

    Returns:
        Unit name to fingerprint.
    """
    computed: dict[str, str] = {}
    for name in order:
        computed[name] = fingerprint_for(layout.unit(name), layout, computed)
    return computed


class BuildCache(Protocol):
    """Remembers successful builds by fingerprint."""

    def get(self, key: str) -> CacheEntry | None:
        """Return the entry for ``key``, or ``None``."""
        ...

    def put(self, entry: CacheEntry) -> None:
        """Store an entry, replacing any entry with the same key."""
        ...

    def evict(self, unit: str) -> int:
        """Forget every entry for one unit, returning how many were removed."""
        ...

    def clear(self) -> int:
        """Forget everything, returning how many entries were removed."""
        ...


class MemoryCache:
    """An in-process cache. Fast, and gone when the process is."""

    __slots__ = ("_entries",)

    def __init__(self, entries: Iterable[CacheEntry] = ()) -> None:
        """Create the cache, optionally pre-populated."""
        self._entries: dict[str, CacheEntry] = {e.key: e for e in entries}

    def get(self, key: str) -> CacheEntry | None:
        """Return the entry for ``key``, or ``None``."""
        return self._entries.get(key)

    def put(self, entry: CacheEntry) -> None:
        """Store an entry."""
        self._entries[entry.key] = entry

    def evict(self, unit: str) -> int:
        """Forget every entry for one unit."""
        doomed = [key for key, entry in self._entries.items() if entry.unit == unit]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    def clear(self) -> int:
        """Forget everything."""
        count = len(self._entries)
        self._entries.clear()
        return count

    def __len__(self) -> int:
        return len(self._entries)


class SqliteCache:
    """A cache that survives the process, in the M2 database."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        """Bind the cache to a database, creating its tables if needed."""
        self._db = db
        with db.transaction() as conn:
            for statement in filter(None, (s.strip() for s in _DDL.split(";"))):
                conn.execute(statement)

    @property
    def db(self) -> Database:
        """The underlying database."""
        return self._db

    def get(self, key: str) -> CacheEntry | None:
        """Return the entry for ``key``, or ``None``."""
        row = self._db.query_one("SELECT * FROM build_cache WHERE key = ?", (key,))
        return _row_to_entry(row) if row is not None else None

    def put(self, entry: CacheEntry) -> None:
        """Store an entry and its artifact rows, atomically."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO build_cache "
                "(key, unit, status, command, duration_s, output, artifacts_json, "
                " diagnostics_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.key,
                    entry.unit,
                    entry.status.value,
                    entry.command,
                    entry.duration_s,
                    entry.output,
                    json.dumps([_artifact_row(a) for a in entry.artifacts], sort_keys=True),
                    json.dumps(
                        [_diagnostic_row(d) for d in entry.diagnostics], sort_keys=True
                    ),
                ),
            )
            conn.execute("DELETE FROM build_artifacts WHERE key = ?", (entry.key,))
            for artifact in entry.artifacts:
                conn.execute(
                    "INSERT OR REPLACE INTO build_artifacts "
                    "(key, unit, path, digest, size) VALUES (?, ?, ?, ?, ?)",
                    (
                        entry.key,
                        entry.unit,
                        artifact.path,
                        artifact.digest,
                        artifact.size,
                    ),
                )

    def evict(self, unit: str) -> int:
        """Forget every entry for one unit."""
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM build_cache WHERE unit = ?", (unit,))
            removed = cursor.rowcount
            conn.execute("DELETE FROM build_artifacts WHERE unit = ?", (unit,))
        return max(0, removed)

    def clear(self) -> int:
        """Forget everything."""
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM build_cache")
            removed = cursor.rowcount
            conn.execute("DELETE FROM build_artifacts")
        return max(0, removed)

    def artifacts_of(self, unit: str) -> tuple[Artifact, ...]:
        """Every artifact recorded for a unit, newest key last, in path order."""
        rows = self._db.query(
            "SELECT * FROM build_artifacts WHERE unit = ? ORDER BY path ASC", (unit,)
        )
        return tuple(
            Artifact(
                path=str(row["path"]),
                digest=str(row["digest"]),
                size=int(row["size"]),
                unit=str(row["unit"]),
            )
            for row in rows
        )

    def __len__(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM build_cache")
        return int(row["n"]) if row is not None else 0


def _artifact_row(artifact: Artifact) -> dict[str, object]:
    return {
        "path": artifact.path,
        "digest": artifact.digest,
        "size": artifact.size,
        "unit": artifact.unit,
    }


def _diagnostic_row(diagnostic: Diagnostic) -> dict[str, object]:
    return {
        "message": diagnostic.message,
        "file": diagnostic.file,
        "line": diagnostic.line,
        "column": diagnostic.column,
        "severity": diagnostic.severity.value,
        "code": diagnostic.code,
        "unit": diagnostic.unit,
    }


def _row_to_entry(row: sqlite3.Row) -> CacheEntry:
    """Rebuild an entry from its stored row."""
    artifacts = tuple(
        Artifact(
            path=str(item["path"]),
            digest=str(item["digest"]),
            size=int(item.get("size", 0) or 0),
            unit=str(item.get("unit", "")),
        )
        for item in json.loads(row["artifacts_json"])
    )
    diagnostics = tuple(
        Diagnostic(
            message=str(item.get("message", "")),
            file=str(item.get("file", "")),
            line=item.get("line"),
            column=item.get("column"),
            severity=Severity(item.get("severity", "error")),
            code=str(item.get("code", "")),
            unit=str(item.get("unit", "")),
        )
        for item in json.loads(row["diagnostics_json"])
    )
    return CacheEntry(
        key=str(row["key"]),
        unit=str(row["unit"]),
        status=BuildStatus(str(row["status"])),
        artifacts=artifacts,
        diagnostics=diagnostics,
        command=str(row["command"]),
        duration_s=float(row["duration_s"]),
        output=str(row["output"]),
    )


@dataclass(frozen=True, slots=True)
class ArtifactTracker:
    """Finds and verifies the files a build produced.

    Artifact patterns are paths or directory prefixes, matched against what is
    actually on disk after the command ran. Nothing is inferred from the build
    output: a tool that prints what it wrote is a tool that will eventually
    print it wrong.
    """

    ignored_suffixes: tuple[str, ...] = ()

    def collect(self, root: Path, unit: BuildUnit) -> tuple[Artifact, ...]:
        """Digest everything a unit's artifact patterns match, in path order."""
        found: dict[str, Artifact] = {}
        for pattern in unit.artifacts:
            target = root / pattern
            if target.is_file():
                self._add(found, root, target, unit.name)
            elif target.is_dir():
                for path in sorted(target.rglob("*")):
                    if path.is_file():
                        self._add(found, root, path, unit.name)
        return tuple(found[key] for key in sorted(found))

    def _add(
        self, found: dict[str, Artifact], root: Path, path: Path, unit: str
    ) -> None:
        """Record one file, unless its suffix is ignored."""
        if path.suffix in self.ignored_suffixes:
            return
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        found[relative] = Artifact(
            path=relative, digest=digest_bytes(data), size=len(data), unit=unit
        )

    @staticmethod
    def missing(root: Path, artifacts: Iterable[Artifact]) -> tuple[str, ...]:
        """Which recorded artifacts are no longer on disk, in path order."""
        return tuple(
            sorted(a.path for a in artifacts if not (root / a.path).is_file())
        )

    @staticmethod
    def drifted(root: Path, artifacts: Iterable[Artifact]) -> tuple[str, ...]:
        """Which artifacts exist but no longer match their recorded digest."""
        changed: list[str] = []
        for artifact in artifacts:
            path = root / artifact.path
            if path.is_file() and digest_bytes(path.read_bytes()) != artifact.digest:
                changed.append(artifact.path)
        return tuple(sorted(changed))
