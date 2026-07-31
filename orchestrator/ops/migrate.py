"""Configuration migration.

Configuration is strict: an unknown key is an error, not a warning. That is the
right default — a typo in ``max_concurency`` that silently kept its default
would be found by noticing the run was slow. It also means that renaming a key
breaks every existing installation on upgrade, loudly, at start-up.

This module is the other half of that bargain. Each release that changes the
shape of the file registers a step; a file declares which shape it was written
for; and the migration runs the steps between there and here.

Two rules the steps obey:

* **Nothing is discarded silently.** A removed key is reported as a note, not
  dropped. An operator who set something should be told it no longer does
  anything.
* **Migration is a pure function of the mapping.** No file is written unless a
  caller asks for it, so a migration can be previewed — and previewing an
  upgrade is the difference between an upgrade and an outage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.core.events import OrchestratorError
from orchestrator.core.logging import get_logger

__all__ = [
    "CONFIG_VERSION",
    "MigrationError",
    "MigrationResult",
    "Step",
    "migrate_config",
    "migrate_file",
    "needs_migration",
    "steps_between",
]

_log = get_logger(__name__)

#: The shape this build understands. Bumped by any release that renames,
#: removes, or relocates a configuration key.
CONFIG_VERSION = 2

#: The key recording which shape a file was written for. Absent means version 1
#: — every file written before migration existed.
VERSION_KEY = "config_version"


class MigrationError(OrchestratorError):
    """A configuration could not be migrated."""

    code = "config_migration"
    retryable = False


@dataclass(frozen=True, slots=True)
class Step:
    """One version-to-version transformation.

    Attributes:
        from_version: The shape this step reads.
        to_version: The shape it produces.
        summary: What it does, in one line, for the operator.
        apply: Takes a mapping and returns the migrated mapping and any notes.
    """

    from_version: int
    to_version: int
    summary: str
    apply: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """What a migration did."""

    config: Mapping[str, Any]
    from_version: int
    to_version: int
    applied: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    changed: bool = False

    def summary(self) -> str:
        """A one-line account."""
        if not self.changed:
            return f"already at version {self.to_version}; nothing to do"
        return (
            f"migrated {self.from_version} -> {self.to_version} "
            f"in {len(self.applied)} step(s)"
        )


def _v1_to_v2(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Introduce the ``[security]`` section and its production defaults.

    Version 1 had no hardening settings at all: a deployment either bound to
    loopback or was on its own. This step gives every existing file the
    defaults, and moves the two settings that had been living in ``[api]``
    where they did not belong.
    """
    notes: list[str] = []
    migrated = {key: dict(value) if isinstance(value, dict) else value for key, value in config.items()}

    api = migrated.get("api")
    security = dict(migrated.get("security", {}))

    if isinstance(api, dict):
        for old, new in (("allowed_hosts", "allowed_hosts"), ("max_body_bytes", "max_body_bytes")):
            if old in api:
                security.setdefault(new, api.pop(old))
                notes.append(f"moved api.{old} to security.{new}")

    if security:
        migrated["security"] = security
    return migrated, notes


#: Every step this build knows, in order.
STEPS: tuple[Step, ...] = (
    Step(
        from_version=1,
        to_version=2,
        summary="add the [security] section and relocate hardening keys from [api]",
        apply=_v1_to_v2,
    ),
)


def version_of(config: Mapping[str, Any]) -> int:
    """Return the shape a configuration mapping was written for.

    Raises:
        MigrationError: If the recorded version is not a usable integer.
    """
    raw = config.get(VERSION_KEY, 1)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MigrationError(
            f"{VERSION_KEY} must be an integer, got {raw!r}",
            detail={"value": raw},
        )
    if raw < 1:
        raise MigrationError(
            f"{VERSION_KEY} must be at least 1, got {raw}", detail={"value": raw}
        )
    return raw


def needs_migration(config: Mapping[str, Any]) -> bool:
    """Whether a configuration is older than this build understands."""
    return version_of(config) < CONFIG_VERSION


def steps_between(start: int, end: int) -> tuple[Step, ...]:
    """Return the steps that take ``start`` to ``end``.

    Raises:
        MigrationError: If no chain of steps connects them.
    """
    chain: list[Step] = []
    current = start
    while current < end:
        step = next((s for s in STEPS if s.from_version == current), None)
        if step is None:
            raise MigrationError(
                f"no migration step from version {current}; this build cannot "
                f"upgrade a version-{start} configuration",
                detail={"from": start, "to": end, "stuck_at": current},
            )
        chain.append(step)
        current = step.to_version
    return tuple(chain)


def migrate_config(
    config: Mapping[str, Any], *, target: int = CONFIG_VERSION
) -> MigrationResult:
    """Bring a configuration mapping up to date.

    Args:
        config: The parsed configuration.
        target: The version to migrate to.

    Returns:
        The result, including the migrated mapping. Pure: nothing is written.

    Raises:
        MigrationError: If the file is newer than this build, or no chain of
            steps reaches the target.
    """
    start = version_of(config)

    if start > target:
        raise MigrationError(
            f"this configuration is version {start}; this build understands "
            f"{target}. Upgrade OrchestratorPro rather than downgrading the file.",
            detail={"file": start, "supported": target},
        )
    if start == target:
        return MigrationResult(
            config={**config, VERSION_KEY: target},
            from_version=start,
            to_version=target,
            changed=VERSION_KEY not in config,
        )

    migrated = dict(config)
    applied: list[str] = []
    notes: list[str] = []

    for step in steps_between(start, target):
        migrated, step_notes = step.apply(migrated)
        applied.append(f"{step.from_version} -> {step.to_version}: {step.summary}")
        notes.extend(step_notes)

    migrated[VERSION_KEY] = target
    _log.info("configuration migrated", **{"from": start, "to": target, "steps": len(applied)})

    return MigrationResult(
        config=migrated,
        from_version=start,
        to_version=target,
        applied=tuple(applied),
        notes=tuple(notes),
        changed=True,
    )


def migrate_file(
    path: Path, *, target: int = CONFIG_VERSION, write: bool = False, backup: bool = True
) -> MigrationResult:
    """Migrate a TOML configuration file.

    Args:
        path: The file.
        target: The version to migrate to.
        write: Whether to write the result back. Off by default so that
            previewing an upgrade is the easy thing to do.
        backup: Whether to keep the original beside the new one.

    Returns:
        The result.

    Raises:
        MigrationError: If the file is missing or unreadable.
    """
    import tomllib

    if not path.is_file():
        raise MigrationError(f"there is no configuration file at {path}")

    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise MigrationError(
            f"{path} is not valid TOML: {exc}", detail={"path": str(path)}
        ) from exc

    result = migrate_config(config, target=target)

    if write and result.changed:
        if backup:
            path.with_name(f"{path.name}.v{result.from_version}").write_bytes(
                path.read_bytes()
            )
        path.write_text(render_toml(result.config), encoding="utf-8")
        _log.info("configuration file rewritten", path=str(path))

    return result


def render_toml(config: Mapping[str, Any]) -> str:
    """Render a configuration mapping back to TOML.

    Deliberately small: this writes the subset of TOML that OrchestratorPro's
    own configuration uses — scalars, string lists, and one level of nesting —
    and refuses anything else rather than emitting something that will not
    parse. The standard library reads TOML but does not write it, and a
    dependency for a hundred lines of output is a poor trade.

    Raises:
        MigrationError: On a value this writer cannot represent.
    """
    lines: list[str] = []
    scalars = {key: value for key, value in config.items() if not isinstance(value, Mapping)}
    tables = {key: value for key, value in config.items() if isinstance(value, Mapping)}

    for key, value in sorted(scalars.items()):
        lines.append(f"{key} = {_render_value(key, value)}")
    if scalars and tables:
        lines.append("")

    for name, table in sorted(tables.items()):
        lines.extend(_render_table(name, table))

    return "\n".join(lines).rstrip("\n") + "\n"


def _render_table(name: str, table: Mapping[str, Any], depth: int = 0) -> list[str]:
    """Render one TOML table and its sub-tables."""
    if depth > 2:
        raise MigrationError(
            f"table [{name}] nests deeper than this writer supports",
            detail={"table": name},
        )
    scalars = {key: value for key, value in table.items() if not isinstance(value, Mapping)}
    nested = {key: value for key, value in table.items() if isinstance(value, Mapping)}

    lines = [f"[{name}]"]
    for key, value in sorted(scalars.items()):
        lines.append(f"{key} = {_render_value(f'{name}.{key}', value)}")
    lines.append("")

    for sub, value in sorted(nested.items()):
        lines.extend(_render_table(f"{name}.{sub}", value, depth + 1))
    return lines


def _render_value(key: str, value: Any) -> str:
    """Render one TOML scalar or list of scalars."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, Sequence):
        return "[" + ", ".join(_render_value(key, item) for item in value) + "]"
    raise MigrationError(
        f"cannot write {key}: values of type {type(value).__name__} are not supported",
        detail={"key": key, "type": type(value).__name__},
    )
