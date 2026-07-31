"""Project analysis and dependency analysis.

Two questions, answered before anything is built:

:class:`ProjectAnalyzer`
    *What is here?* Walks a project root, records every source file by content
    digest, and — when nobody has written a manifest — proposes a set of build
    units from the shape of the tree.
:class:`DependencyAnalyzer`
    *What depends on what?* Turns declared dependencies, and for Python the
    import graph, into a validated :class:`UnitGraph`.

Detection is a starting point, never an authority. A manifest always wins, and
an inferred edge is only ever added to declared ones — never used to remove one.
A build system that quietly disagrees with what the operator wrote is worse than
one that cannot guess at all.

The filesystem walk runs on a worker thread: it is the one genuinely blocking
operation in this package, and a thousand-file repository would otherwise stall
the event loop (``CLAUDE.md``, async-first).
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from orchestrator.builder.model import (
    BuildConfigError,
    BuildUnit,
    BuilderError,
    ProjectKind,
    ProjectLayout,
    SourceFile,
    digest_bytes,
)

__all__ = [
    "DEFAULT_IGNORED_DIRS",
    "BuildCycleError",
    "DependencyAnalyzer",
    "ProjectAnalyzer",
    "UnitGraph",
    "changed_units",
    "detect_kind",
]

#: Directories that are never sources. Scanning a virtualenv would swamp the
#: digest set with files the project does not own and cannot rebuild.
DEFAULT_IGNORED_DIRS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    "dist",
    "build",
    ".tox",
    ".idea",
    ".vscode",
)

#: Marker file to the kind it implies, and the build command that usually goes
#: with it. Ordered: the first match wins, so a Rust workspace with a stray
#: ``Makefile`` is still a Rust project.
_MARKERS: tuple[tuple[str, ProjectKind, str], ...] = (
    ("Cargo.toml", ProjectKind.RUST, "cargo build"),
    ("go.mod", ProjectKind.GO, "go build ./..."),
    ("package.json", ProjectKind.NODE, "npm run build"),
    ("pyproject.toml", ProjectKind.PYTHON, "python -m compileall -q ."),
    ("setup.py", ProjectKind.PYTHON, "python -m compileall -q ."),
    ("Makefile", ProjectKind.MAKE, "make"),
)

_SOURCE_SUFFIXES: Mapping[ProjectKind, tuple[str, ...]] = {
    ProjectKind.PYTHON: (".py", ".pyi"),
    ProjectKind.NODE: (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
    ProjectKind.RUST: (".rs",),
    ProjectKind.GO: (".go",),
    ProjectKind.MAKE: (),
    ProjectKind.UNKNOWN: (),
}


class BuildCycleError(BuilderError):
    """The build units form a cycle and cannot be ordered."""

    code = "build_cycle"
    retryable = False


def detect_kind(root: Path) -> tuple[ProjectKind, str]:
    """Guess what kind of project ``root`` holds.

    Args:
        root: The project root.

    Returns:
        The kind and a default build command for it. An unrecognized project
        yields :attr:`ProjectKind.UNKNOWN` and an empty command — deliberately
        unusable, so that a project nobody described fails loudly at planning
        rather than silently building nothing.
    """
    for marker, kind, command in _MARKERS:
        if (root / marker).is_file():
            return kind, command
    return ProjectKind.UNKNOWN, ""


class ProjectAnalyzer:
    """Scans a project root into a :class:`ProjectLayout`."""

    __slots__ = ("_ignored", "_max_files", "_suffixes")

    def __init__(
        self,
        *,
        ignored_dirs: Sequence[str] = DEFAULT_IGNORED_DIRS,
        suffixes: Sequence[str] | None = None,
        max_files: int = 50_000,
    ) -> None:
        """Create the analyzer.

        Args:
            ignored_dirs: Directory names never descended into.
            suffixes: File suffixes to treat as sources. ``None`` picks them
                from the detected project kind.
            max_files: Refuse to scan a tree larger than this. A runaway walk
                over a mounted volume is a hang, and a hang with no message is
                the worst failure mode available.
        """
        self._ignored = frozenset(ignored_dirs)
        self._suffixes = tuple(suffixes) if suffixes is not None else None
        self._max_files = max_files

    async def analyze(
        self,
        root: Path,
        *,
        manifest: Sequence[BuildUnit] | None = None,
    ) -> ProjectLayout:
        """Scan ``root`` and return what is there.

        Args:
            root: The project root.
            manifest: Units the operator declared. When given, they are used
                verbatim and nothing is inferred.

        Returns:
            The layout.

        Raises:
            BuilderError: If the root does not exist or the tree is too large.
        """
        if not root.is_dir():
            raise BuilderError(
                f"{root} is not a directory; there is nothing to analyze",
                detail={"root": str(root)},
            )
        return await asyncio.to_thread(self._analyze_sync, root, manifest)

    def _analyze_sync(
        self, root: Path, manifest: Sequence[BuildUnit] | None
    ) -> ProjectLayout:
        """The blocking half of :meth:`analyze`."""
        kind, default_command = detect_kind(root)
        suffixes = self._suffixes or _SOURCE_SUFFIXES.get(kind, ())
        sources = self._scan(root, suffixes)

        units = (
            tuple(manifest)
            if manifest is not None
            else self._infer_units(root, kind, default_command, sources)
        )
        return ProjectLayout(
            root=root,
            kind=kind,
            units=units,
            sources=sources,
            ignored_dirs=tuple(sorted(self._ignored)),
        )

    def _scan(self, root: Path, suffixes: Sequence[str]) -> dict[str, SourceFile]:
        """Digest every source file under ``root``."""
        found: dict[str, SourceFile] = {}
        for path in sorted(root.rglob("*")):
            if len(found) >= self._max_files:
                raise BuilderError(
                    f"{root} holds more than {self._max_files} source files; "
                    "narrow the scan with ignored_dirs or a manifest",
                    detail={"root": str(root), "max_files": self._max_files},
                )
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:  # pragma: no cover - rglob cannot produce this
                continue
            if any(part in self._ignored for part in relative.parts[:-1]):
                continue
            if suffixes and path.suffix not in suffixes:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                # A file that cannot be read is not a source we can fingerprint.
                # Skipping it beats failing the whole scan on one broken symlink.
                continue
            key = relative.as_posix()
            found[key] = SourceFile(path=key, digest=digest_bytes(data), size=len(data))
        return found

    def _infer_units(
        self,
        root: Path,
        kind: ProjectKind,
        default_command: str,
        sources: Mapping[str, SourceFile],
    ) -> tuple[BuildUnit, ...]:
        """Propose units for a project nobody has described.

        For Python, one unit per top-level package, because that is the level at
        which people actually reason about a rebuild. Everything else gets a
        single whole-project unit: a wrong split is worse than no split.
        """
        if not sources:
            return ()
        if kind is ProjectKind.PYTHON:
            packages = self._python_packages(root, sources)
            if packages:
                return tuple(
                    BuildUnit(
                        name=name,
                        command=f"python -m compileall -q {prefix}",
                        sources=(prefix,),
                        incremental=True,
                    )
                    for name, prefix in packages
                )
        if not default_command:
            return ()
        return (
            BuildUnit(
                name=root.name or "project",
                command=default_command,
                sources=tuple(sorted({p.split("/")[0] for p in sources})),
            ),
        )

    @staticmethod
    def _python_packages(
        root: Path, sources: Mapping[str, SourceFile]
    ) -> tuple[tuple[str, str], ...]:
        """Find importable top-level packages, under ``src/`` or at the root."""
        found: dict[str, str] = {}
        for path in sources:
            parts = path.split("/")
            if parts[-1] != "__init__.py":
                continue
            if len(parts) == 2:
                found.setdefault(parts[0], parts[0])
            elif len(parts) == 3 and parts[0] == "src":
                found.setdefault(parts[1], f"src/{parts[1]}")
        return tuple(sorted(found.items()))


class UnitGraph:
    """An immutable, validated DAG over build units."""

    __slots__ = ("_dependents", "_order", "_units")

    def __init__(self, units: Iterable[BuildUnit]) -> None:
        """Build and validate the graph.

        Raises:
            BuildConfigError: If a dependency names a unit that does not exist.
            BuildCycleError: If the edges form a cycle.
        """
        indexed: dict[str, BuildUnit] = {}
        for unit in units:
            if unit.name in indexed:
                raise BuildConfigError(
                    f"two build units share a name: {unit.name!r}",
                    detail={"unit": unit.name},
                )
            indexed[unit.name] = unit

        for unit in indexed.values():
            missing = sorted(set(unit.depends_on) - set(indexed))
            if missing:
                raise BuildConfigError(
                    f"build unit {unit.name!r} depends on unknown unit(s): "
                    f"{', '.join(missing)}",
                    detail={"unit": unit.name, "missing": missing},
                )

        self._units = indexed
        self._order = self._topological_order()
        self._dependents = self._build_dependents()

    def _topological_order(self) -> tuple[str, ...]:
        """Order units so every dependency precedes its dependents."""
        indegree = {name: len(unit.depends_on) for name, unit in self._units.items()}
        dependents: dict[str, list[str]] = {name: [] for name in self._units}
        for name, unit in self._units.items():
            for dependency in unit.depends_on:
                dependents[dependency].append(name)

        ready = sorted(name for name, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while ready:
            name = ready.pop(0)
            order.append(name)
            for child in sorted(dependents[name]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()

        if len(order) != len(self._units):
            stuck = sorted(set(self._units) - set(order))
            raise BuildCycleError(
                f"build units form a cycle: {', '.join(stuck)}",
                detail={"units": stuck},
            )
        return tuple(order)

    def _build_dependents(self) -> Mapping[str, tuple[str, ...]]:
        """Reverse edges, for propagating a rebuild downstream."""
        reverse: dict[str, list[str]] = {name: [] for name in self._units}
        for name, unit in self._units.items():
            for dependency in unit.depends_on:
                reverse[dependency].append(name)
        return {name: tuple(sorted(children)) for name, children in reverse.items()}

    def __contains__(self, name: object) -> bool:
        return name in self._units

    def __len__(self) -> int:
        return len(self._units)

    @property
    def names(self) -> tuple[str, ...]:
        """Every unit name, in topological order."""
        return self._order

    @property
    def units(self) -> tuple[BuildUnit, ...]:
        """Every unit, in topological order."""
        return tuple(self._units[name] for name in self._order)

    def get(self, name: str) -> BuildUnit:
        """Return one unit.

        Raises:
            BuildConfigError: If it is not in the graph.
        """
        try:
            return self._units[name]
        except KeyError:
            raise BuildConfigError(
                f"there is no build unit named {name!r}",
                detail={"unit": name, "known": list(self._order)},
            ) from None

    def dependencies(self, name: str) -> tuple[str, ...]:
        """The units this one waits for."""
        return tuple(sorted(self.get(name).depends_on))

    def dependents(self, name: str) -> tuple[str, ...]:
        """The units that wait for this one."""
        self.get(name)
        return self._dependents[name]

    def descendants(self, names: Iterable[str]) -> frozenset[str]:
        """Everything downstream of ``names``, transitively, excluding them.

        This is what makes a rebuild correct: changing a unit invalidates every
        unit built against it, however far away.
        """
        seen: set[str] = set()
        stack = [child for name in names for child in self.dependents(name)]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._dependents[current])
        return frozenset(seen)

    def ancestors(self, names: Iterable[str]) -> frozenset[str]:
        """Everything upstream of ``names``, transitively, excluding them."""
        seen: set[str] = set()
        stack = [dep for name in names for dep in self.get(name).depends_on]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._units[current].depends_on)
        return frozenset(seen)

    def layers(self, only: Iterable[str] | None = None) -> tuple[tuple[str, ...], ...]:
        """Group units into waves that can be built in parallel.

        Args:
            only: Restrict to a subset. Dependencies outside the subset are
                treated as already satisfied — which they are, if the planner
                left them out because they were up to date.
        """
        selected = set(self._order if only is None else only)
        for name in selected:
            self.get(name)

        placed: dict[str, int] = {}
        for name in self._order:
            if name not in selected:
                continue
            depth = 0
            for dependency in self._units[name].depends_on:
                if dependency in placed:
                    depth = max(depth, placed[dependency] + 1)
            placed[name] = depth

        if not placed:
            return ()
        grouped: list[list[str]] = [[] for _ in range(max(placed.values()) + 1)]
        for name, depth in placed.items():
            grouped[depth].append(name)
        return tuple(tuple(sorted(group)) for group in grouped)

    @property
    def max_width(self) -> int:
        """The widest wave, i.e. the most useful concurrency limit."""
        layers = self.layers()
        return max((len(layer) for layer in layers), default=0)


class DependencyAnalyzer:
    """Turns a layout into a validated :class:`UnitGraph`."""

    __slots__ = ("_infer_imports",)

    def __init__(self, *, infer_imports: bool = True) -> None:
        """Create the analyzer.

        Args:
            infer_imports: Whether to add edges implied by Python imports.
                Inference only ever *adds* edges to what was declared.
        """
        self._infer_imports = infer_imports

    async def analyze(self, layout: ProjectLayout) -> UnitGraph:
        """Build the dependency graph for a layout."""
        if not self._infer_imports or layout.kind is not ProjectKind.PYTHON:
            return UnitGraph(layout.units)
        inferred = await asyncio.to_thread(self.infer_python_edges, layout)
        return UnitGraph(self._merge(layout.units, inferred))

    @staticmethod
    def _merge(
        units: Sequence[BuildUnit], edges: Mapping[str, frozenset[str]]
    ) -> tuple[BuildUnit, ...]:
        """Add inferred edges to declared ones, dropping self-edges."""
        from dataclasses import replace

        merged: list[BuildUnit] = []
        for unit in units:
            extra = edges.get(unit.name, frozenset()) - {unit.name}
            combined = tuple(sorted(set(unit.depends_on) | extra))
            merged.append(
                unit if combined == tuple(unit.depends_on) else replace(unit, depends_on=combined)
            )
        return tuple(merged)

    @staticmethod
    def infer_python_edges(layout: ProjectLayout) -> Mapping[str, frozenset[str]]:
        """Read import statements and map them onto units.

        Only top-level module names are considered, and only when they name a
        unit in this project. A dotted import resolves to its root package, and
        an import of something outside the project is ignored — a third-party
        dependency is not a build edge.
        """
        by_module: dict[str, str] = {}
        for unit in layout.units:
            for source in unit.sources:
                module = source.split("/")[-1]
                by_module.setdefault(module, unit.name)
            by_module.setdefault(unit.name, unit.name)

        edges: dict[str, set[str]] = {unit.name: set() for unit in layout.units}
        for path, _source in sorted(layout.sources.items()):
            if not path.endswith(".py"):
                continue
            owner = layout.owner_of(path)
            if owner is None:
                continue
            full = layout.root / path
            try:
                tree = ast.parse(full.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                # Unparseable source is a compile error, which the build will
                # report far better than a dependency analyzer could.
                continue
            for module in _imported_roots(tree):
                target = by_module.get(module)
                if target is not None and target != owner:
                    edges[owner].add(target)

        return {name: frozenset(targets) for name, targets in edges.items()}


def _imported_roots(tree: ast.AST) -> frozenset[str]:
    """Every top-level module name imported anywhere in a parsed file."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return frozenset(roots)


def changed_units(layout: ProjectLayout, changed_paths: Iterable[str]) -> frozenset[str]:
    """Which units own the changed files.

    A changed path that no unit claims is reported by nobody — deliberately.
    Attributing an unowned file to an arbitrary unit would produce rebuilds that
    look random to whoever is watching.
    """
    owners: set[str] = set()
    for path in changed_paths:
        owner = layout.owner_of(path.replace("\\", "/"))
        if owner is not None:
            owners.add(owner)
    return frozenset(owners)
