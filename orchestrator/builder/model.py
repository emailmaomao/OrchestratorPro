"""Domain objects for the build subsystem.

A *build unit* is the smallest thing worth rebuilding on its own: a package, a
crate, a bundle. Units declare their sources, the command that builds them, the
artifacts that command produces, and the units they depend on. Everything else
in this package is a function of those four facts.

Two decisions are worth stating, because the rest of the package rests on them:

* **Change is detected by content, never by timestamp.** A digest survives a
  checkout, a clock skew, and a file copied back into place; an mtime does not.
  A build that reruns because Git rewrote a timestamp is a build that has taught
  its operator to distrust incremental mode.
* **A failed build and a broken build tool are different outcomes.** The same
  distinction the gates draw (FR-4.4): :attr:`BuildStatus.FAILED` is a verdict
  about the code, :attr:`BuildStatus.ERRORED` is a verdict about the harness.
  Reporting a missing compiler as a compilation failure sends the next attempt
  to edit source that was never wrong.

Unit definitions can come from a manifest an agent wrote, so every path here is
validated on construction — no absolutes, no ``..`` — under the same rule that
governs tool arguments (``CLAUDE.md``, security invariants).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from orchestrator.core.events import OrchestratorError

__all__ = [
    "Artifact",
    "BuildReport",
    "BuildStatus",
    "BuildUnit",
    "BuilderError",
    "Diagnostic",
    "ProjectKind",
    "ProjectLayout",
    "Severity",
    "SourceFile",
    "UnitResult",
    "digest_bytes",
    "digest_text",
    "normalize_path",
    "stable_json",
]


class BuilderError(OrchestratorError):
    """A build could not be analyzed, planned, or executed."""

    code = "builder"
    retryable = False


class BuildConfigError(BuilderError):
    """A unit or manifest is not usable as written."""

    code = "build_config"
    retryable = False


class ProjectKind(StrEnum):
    """What kind of project a root looks like.

    Detection is a convenience for a project nobody has described yet. An
    explicit manifest always wins: guessing is a starting point, not an
    authority.
    """

    PYTHON = "python"
    NODE = "node"
    RUST = "rust"
    GO = "go"
    MAKE = "make"
    UNKNOWN = "unknown"


class BuildStatus(StrEnum):
    """How one unit's build resolved."""

    SUCCEEDED = "succeeded"
    CACHED = "cached"
    FAILED = "failed"
    ERRORED = "errored"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    SKIPPED = "skipped"

    @property
    def ok(self) -> bool:
        """Whether the unit's artifacts can be relied on afterwards."""
        return self in (BuildStatus.SUCCEEDED, BuildStatus.CACHED, BuildStatus.SKIPPED)

    @property
    def ran(self) -> bool:
        """Whether the build command was actually executed."""
        return self in (
            BuildStatus.SUCCEEDED,
            BuildStatus.FAILED,
            BuildStatus.ERRORED,
            BuildStatus.TIMED_OUT,
        )

    @property
    def is_harness_problem(self) -> bool:
        """Whether the tooling broke rather than the code (FR-4.4)."""
        return self in (BuildStatus.ERRORED, BuildStatus.TIMED_OUT)


class Severity(StrEnum):
    """How serious one diagnostic is."""

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


def digest_bytes(data: bytes) -> str:
    """Return the content digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    """Return the content digest of text, normalizing line endings.

    A file that differs only in line endings is the same source. Without this
    every checkout on a different platform would look like a total rebuild.
    """
    return digest_bytes(text.replace("\r\n", "\n").encode("utf-8"))


def stable_json(value: Any) -> str:
    """Serialize deterministically, so digests over it are stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def normalize_path(path: str, *, what: str = "path") -> str:
    """Return ``path`` as a repository-relative POSIX path, or raise.

    Args:
        path: The path to normalize.
        what: What is being validated, for the error message.

    Returns:
        The normalized path.

    Raises:
        BuildConfigError: If the path is absolute, escapes the root, or is
            empty. Manifests can be written by an agent, so this is a trust
            boundary, not a tidiness check.
    """
    text = path.strip().replace("\\", "/")
    if not text:
        raise BuildConfigError(f"{what} must not be empty")
    pure = PurePosixPath(text)
    if pure.is_absolute() or text[1:3] == ":/":
        raise BuildConfigError(
            f"{what} must be relative to the project root, got {path!r}",
            detail={"path": path},
        )
    parts = [part for part in pure.parts if part not in (".",)]
    if ".." in parts:
        raise BuildConfigError(
            f"{what} must not escape the project root, got {path!r}",
            detail={"path": path},
        )
    if not parts:
        raise BuildConfigError(f"{what} must not be empty")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One input file, identified by its content."""

    path: str
    digest: str
    size: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_path(self.path, what="source path"))


@dataclass(frozen=True, slots=True)
class BuildUnit:
    """One independently-buildable part of a project.

    Attributes:
        name: Stable identifier, used in plans, caches, and reports.
        command: The build command, run without a shell in the project root.
        sources: Repository-relative paths and directory prefixes that feed it.
        depends_on: Units that must be built first.
        artifacts: Paths and prefixes the command is expected to produce.
        incremental: Whether the tool can build only what changed. A unit that
            cannot is still skipped when nothing changed — the flag governs
            whether a *partial* rebuild is meaningful, and is what a planner
            consults before proposing one.
        timeout_s: Wall-clock limit for the command.
        env: Extra environment variables.
        labels: Free-form tags, used for per-label concurrency caps.
    """

    name: str
    command: str
    sources: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    incremental: bool = True
    timeout_s: float = 600.0
    env: Mapping[str, str] = field(default_factory=dict)
    labels: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise BuildConfigError("a build unit must have a name")
        if not self.command.strip():
            raise BuildConfigError(
                f"build unit {self.name!r} must have a command",
                detail={"unit": self.name},
            )
        if self.name in self.depends_on:
            raise BuildConfigError(
                f"build unit {self.name!r} depends on itself",
                detail={"unit": self.name},
            )
        if len(set(self.depends_on)) != len(self.depends_on):
            raise BuildConfigError(
                f"build unit {self.name!r} lists a dependency twice",
                detail={"unit": self.name},
            )
        if self.timeout_s <= 0:
            raise BuildConfigError(
                f"build unit {self.name!r} must have a positive timeout, got "
                f"{self.timeout_s}",
                detail={"unit": self.name},
            )
        object.__setattr__(
            self,
            "sources",
            tuple(normalize_path(p, what="source path") for p in self.sources),
        )
        object.__setattr__(
            self,
            "artifacts",
            tuple(normalize_path(p, what="artifact path") for p in self.artifacts),
        )

    def owns(self, path: str) -> bool:
        """Whether ``path`` is one of this unit's sources.

        A source entry is a path or a directory prefix, so a unit can name a
        package without enumerating every file inside it.
        """
        candidate = path.replace("\\", "/")
        return any(
            candidate == source or candidate.startswith(f"{source}/")
            for source in self.sources
        )

    def identity(self) -> str:
        """The part of the unit that changes what a build *means*.

        Deliberately excludes ``timeout_s`` and ``labels``: raising a timeout
        does not invalidate a cached artifact, and pretending it does would cost
        a full rebuild for a scheduling tweak.
        """
        return stable_json(
            {
                "name": self.name,
                "command": self.command,
                "sources": sorted(self.sources),
                "depends_on": sorted(self.depends_on),
                "artifacts": sorted(self.artifacts),
                "incremental": self.incremental,
                "env": dict(sorted(self.env.items())),
            }
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    """One output file, identified by its content."""

    path: str
    digest: str
    size: int = 0
    unit: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path", normalize_path(self.path, what="artifact path")
        )

    def exists_in(self, root: Path) -> bool:
        """Whether the file is still present under ``root``."""
        return (root / self.path).is_file()


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One message a build tool emitted about one place in the source."""

    message: str
    file: str = ""
    line: int | None = None
    column: int | None = None
    severity: Severity = Severity.ERROR
    code: str = ""
    unit: str = ""

    @property
    def is_error(self) -> bool:
        """Whether this diagnostic is what failed the build."""
        return self.severity is Severity.ERROR

    def location(self) -> str:
        """A ``file:line:col`` locator, as much of it as is known."""
        if not self.file:
            return ""
        parts = [self.file]
        if self.line is not None:
            parts.append(str(self.line))
            if self.column is not None:
                parts.append(str(self.column))
        return ":".join(parts)

    def render(self) -> str:
        """A single line, in the shape compilers already print."""
        where = self.location()
        code = f" [{self.code}]" if self.code else ""
        prefix = f"{where}: " if where else ""
        return f"{prefix}{self.severity.value}{code}: {self.message}".strip()


@dataclass(frozen=True, slots=True)
class UnitResult:
    """What building one unit produced."""

    unit: str
    status: BuildStatus
    fingerprint: str = ""
    diagnostics: tuple[Diagnostic, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    duration_s: float = 0.0
    exit_code: int | None = None
    command: str = ""
    output: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Whether downstream units may rely on this one."""
        return self.status.ok

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """Only the diagnostics that failed the build."""
        return tuple(d for d in self.diagnostics if d.is_error)

    def summary(self) -> str:
        """A one-line account of the unit."""
        if self.status is BuildStatus.CACHED:
            return f"{self.unit}: up to date"
        if self.status is BuildStatus.SUCCEEDED:
            return f"{self.unit}: built in {self.duration_s:.1f}s"
        if self.status.is_harness_problem:
            return f"{self.unit}: build tool {self.status.value} — {self.reason}".rstrip(
                " —"
            )
        if self.status is BuildStatus.BLOCKED:
            return f"{self.unit}: not built — {self.reason or 'a dependency failed'}"
        count = len(self.errors)
        return f"{self.unit}: {self.status.value} with {count} error(s)"

    def feedback(self, *, max_errors: int = 10) -> str:
        """Render the result as instructions for whoever must fix it.

        A harness problem says so explicitly, for the same reason a broken test
        runner does: an attempt told "the build failed" will start editing code
        that compiles perfectly well.
        """
        lines = [self.summary()]
        if self.status.is_harness_problem:
            lines.append(
                "The build tool itself did not run correctly. Do not change "
                "source code to satisfy it; fix the build environment or report "
                "it."
            )
            if self.output.strip():
                lines.append(self.output.strip()[-1000:])
            return "\n".join(lines)

        errors = self.errors[:max_errors]
        lines.extend(f"  {d.render()}" for d in errors)
        if len(self.errors) > max_errors:
            lines.append(f"  ... and {len(self.errors) - max_errors} more")
        if not errors and self.output.strip():
            lines.append(self.output.strip()[-1000:])
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class BuildReport:
    """What one build produced, across every unit in the plan."""

    results: tuple[UnitResult, ...] = ()
    cancelled: bool = False
    duration_s: float = 0.0

    @property
    def by_unit(self) -> Mapping[str, UnitResult]:
        """Results keyed by unit name."""
        return {result.unit: result for result in self.results}

    @property
    def ok(self) -> bool:
        """Whether every unit in the plan is usable and nothing was cut short."""
        return not self.cancelled and all(result.ok for result in self.results)

    @property
    def failed_units(self) -> tuple[str, ...]:
        """Units that did not build, in name order."""
        return tuple(sorted(r.unit for r in self.results if not r.ok))

    @property
    def rebuilt_units(self) -> tuple[str, ...]:
        """Units whose build command actually ran and succeeded."""
        return tuple(
            sorted(r.unit for r in self.results if r.status is BuildStatus.SUCCEEDED)
        )

    @property
    def cached_units(self) -> tuple[str, ...]:
        """Units that were already up to date."""
        return tuple(
            sorted(r.unit for r in self.results if r.status is BuildStatus.CACHED)
        )

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        """Every diagnostic, in unit order."""
        return tuple(d for result in self.results for d in result.diagnostics)

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        """Every artifact produced or carried forward."""
        return tuple(a for result in self.results for a in result.artifacts)

    @property
    def harness_problems(self) -> tuple[str, ...]:
        """Units whose build tool broke, as distinct from failing (FR-4.4)."""
        return tuple(
            sorted(r.unit for r in self.results if r.status.is_harness_problem)
        )

    def summary(self) -> str:
        """A one-line account of the build."""
        if self.cancelled:
            return "build cancelled"
        if not self.results:
            return "nothing to build"
        parts = [f"{len(self.rebuilt_units)} rebuilt", f"{len(self.cached_units)} cached"]
        if self.failed_units:
            parts.append(f"{len(self.failed_units)} failed")
        return ", ".join(parts) + f" in {self.duration_s:.1f}s"

    def feedback(self, *, max_units: int = 5) -> str:
        """Render the failures as instructions for the next attempt."""
        failures = [r for r in self.results if not r.ok and r.status.ran]
        blocked = [r for r in self.results if r.status is BuildStatus.BLOCKED]
        lines = [r.feedback() for r in failures[:max_units]]
        if blocked:
            names = ", ".join(sorted(r.unit for r in blocked))
            lines.append(f"Not attempted because a dependency failed: {names}")
        return "\n\n".join(lines)


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    """A project as the builder understands it."""

    root: Path
    kind: ProjectKind = ProjectKind.UNKNOWN
    units: tuple[BuildUnit, ...] = ()
    sources: Mapping[str, SourceFile] = field(default_factory=dict)
    ignored_dirs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        names = [unit.name for unit in self.units]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise BuildConfigError(
                f"two build units share a name: {', '.join(duplicates)}",
                detail={"duplicates": duplicates},
            )

    @property
    def unit_names(self) -> tuple[str, ...]:
        """Every unit name, in declaration order."""
        return tuple(unit.name for unit in self.units)

    def unit(self, name: str) -> BuildUnit:
        """Return one unit by name.

        Raises:
            BuildConfigError: If no such unit exists.
        """
        for unit in self.units:
            if unit.name == name:
                return unit
        raise BuildConfigError(
            f"there is no build unit named {name!r}",
            detail={"unit": name, "known": list(self.unit_names)},
        )

    def sources_of(self, name: str) -> tuple[SourceFile, ...]:
        """Every source file belonging to a unit, in path order."""
        unit = self.unit(name)
        return tuple(
            self.sources[path] for path in sorted(self.sources) if unit.owns(path)
        )

    def owner_of(self, path: str) -> str | None:
        """Which unit owns a path, if any.

        The most specific claim wins, so a nested unit is not shadowed by the
        parent that also lists its directory.
        """
        candidate = path.replace("\\", "/")
        best: tuple[int, str] | None = None
        for unit in self.units:
            for source in unit.sources:
                if candidate == source or candidate.startswith(f"{source}/"):
                    if best is None or len(source) > best[0]:
                        best = (len(source), unit.name)
        return best[1] if best else None

    def with_units(self, units: Iterable[BuildUnit]) -> ProjectLayout:
        """Return a copy carrying a different set of units."""
        return ProjectLayout(
            root=self.root,
            kind=self.kind,
            units=tuple(units),
            sources=dict(self.sources),
            ignored_dirs=self.ignored_dirs,
        )

    def digest_of(self, paths: Sequence[str]) -> str:
        """A single digest over several source paths, order-independent."""
        return digest_text(
            stable_json(
                {p: self.sources[p].digest for p in sorted(set(paths)) if p in self.sources}
            )
        )
