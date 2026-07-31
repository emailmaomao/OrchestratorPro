"""Tests for project analysis and dependency analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.builder.analysis import (
    BuildCycleError,
    DependencyAnalyzer,
    ProjectAnalyzer,
    UnitGraph,
    changed_units,
    detect_kind,
)
from orchestrator.builder.model import (
    BuildConfigError,
    BuilderError,
    ProjectKind,
    ProjectLayout,
)

from tests.builder.conftest import PYTHON_PROJECT, layout, run, unit, write_tree


class TestDetectKind:
    """Detection is a starting point for a project nobody described."""

    @pytest.mark.parametrize(
        ("marker", "kind"),
        [
            ("Cargo.toml", ProjectKind.RUST),
            ("go.mod", ProjectKind.GO),
            ("package.json", ProjectKind.NODE),
            ("pyproject.toml", ProjectKind.PYTHON),
            ("setup.py", ProjectKind.PYTHON),
            ("Makefile", ProjectKind.MAKE),
        ],
    )
    def test_markers(self, project: Path, marker: str, kind: ProjectKind) -> None:
        (project / marker).write_text("x", encoding="utf-8")
        assert detect_kind(project)[0] is kind

    def test_an_unmarked_directory_is_unknown(self, project: Path) -> None:
        kind, command = detect_kind(project)
        assert kind is ProjectKind.UNKNOWN
        assert command == ""

    def test_a_specific_marker_wins_over_a_makefile(self, project: Path) -> None:
        """A Rust workspace with a helper Makefile is still a Rust project."""
        (project / "Cargo.toml").write_text("x", encoding="utf-8")
        (project / "Makefile").write_text("x", encoding="utf-8")
        assert detect_kind(project)[0] is ProjectKind.RUST


class TestProjectAnalyzer:
    """What is actually here, by content."""

    def test_a_missing_root_is_refused(self, tmp_dir: Path) -> None:
        with pytest.raises(BuilderError, match="not a directory"):
            run(ProjectAnalyzer().analyze(tmp_dir / "nope"))

    def test_sources_are_digested(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        result = run(ProjectAnalyzer().analyze(project))

        assert "app/main.py" in result.sources
        assert result.sources["app/main.py"].digest
        assert result.sources["app/main.py"].size > 0

    def test_non_source_files_are_left_out(self, project: Path) -> None:
        write_tree(project, {**PYTHON_PROJECT, "README.md": "hi"})
        result = run(ProjectAnalyzer().analyze(project))
        assert "README.md" not in result.sources

    def test_ignored_directories_are_not_scanned(self, project: Path) -> None:
        write_tree(
            project,
            {**PYTHON_PROJECT, ".venv/lib/thing.py": "x", "__pycache__/c.py": "x"},
        )
        result = run(ProjectAnalyzer().analyze(project))

        assert not any(path.startswith(".venv/") for path in result.sources)
        assert not any("__pycache__" in path for path in result.sources)

    def test_a_huge_tree_is_refused_rather_than_hanging(self, project: Path) -> None:
        write_tree(project, {f"pkg/f{i}.py": "x" for i in range(6)})
        with pytest.raises(BuilderError, match="more than 3 source files"):
            run(ProjectAnalyzer(max_files=3, suffixes=(".py",)).analyze(project))

    def test_python_packages_become_units(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        result = run(ProjectAnalyzer().analyze(project))
        assert sorted(result.unit_names) == ["app", "core"]

    def test_a_src_layout_is_recognized(self, project: Path) -> None:
        write_tree(
            project,
            {
                "pyproject.toml": "x",
                "src/pkg/__init__.py": "",
                "src/pkg/mod.py": "x = 1\n",
            },
        )
        result = run(ProjectAnalyzer().analyze(project))

        assert result.unit_names == ("pkg",)
        assert result.unit("pkg").sources == ("src/pkg",)

    def test_a_manifest_wins_over_inference(self, project: Path) -> None:
        """An operator who described the project is not second-guessed."""
        write_tree(project, PYTHON_PROJECT)
        declared = (unit("everything", sources=("app", "core")),)
        result = run(ProjectAnalyzer().analyze(project, manifest=declared))

        assert result.unit_names == ("everything",)

    def test_an_unrecognized_project_proposes_nothing(self, project: Path) -> None:
        """Better no units than units that build the wrong thing."""
        write_tree(project, {"thing.txt": "hello"})
        assert run(ProjectAnalyzer().analyze(project)).units == ()

    def test_a_non_python_project_gets_one_unit(self, project: Path) -> None:
        write_tree(project, {"Cargo.toml": "x", "src/lib.rs": "fn main() {}"})
        result = run(ProjectAnalyzer().analyze(project))

        assert len(result.units) == 1
        assert result.units[0].command == "cargo build"

    def test_the_same_tree_analyzes_identically(self, project: Path) -> None:
        """Nothing about the walk may depend on order or time."""
        write_tree(project, PYTHON_PROJECT)
        one = run(ProjectAnalyzer().analyze(project))
        two = run(ProjectAnalyzer().analyze(project))
        assert {p: s.digest for p, s in one.sources.items()} == {
            p: s.digest for p, s in two.sources.items()
        }

    def test_an_edit_changes_the_digest(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        before = run(ProjectAnalyzer().analyze(project)).sources["core/util.py"].digest
        (project / "core/util.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
        after = run(ProjectAnalyzer().analyze(project)).sources["core/util.py"].digest

        assert before != after


class TestUnitGraph:
    """Ordering, and the reverse edges a rebuild depends on."""

    def _diamond(self) -> UnitGraph:
        return UnitGraph(
            [
                unit("root"),
                unit("left", depends_on=("root",)),
                unit("right", depends_on=("root",)),
                unit("join", depends_on=("left", "right")),
            ]
        )

    def test_dependencies_precede_dependents(self) -> None:
        names = self._diamond().names
        assert names.index("root") < names.index("left") < names.index("join")

    def test_a_cycle_is_refused(self) -> None:
        with pytest.raises(BuildCycleError, match="cycle"):
            UnitGraph([unit("a", depends_on=("b",)), unit("b", depends_on=("a",))])

    def test_an_unknown_dependency_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="unknown unit"):
            UnitGraph([unit("a", depends_on=("ghost",))])

    def test_a_duplicate_name_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="share a name"):
            UnitGraph([unit("a"), unit("a")])

    def test_dependents_are_the_reverse_edges(self) -> None:
        assert self._diamond().dependents("root") == ("left", "right")

    def test_descendants_are_transitive(self) -> None:
        """This is the rule an incremental build most often gets wrong."""
        assert self._diamond().descendants(["root"]) == frozenset(
            {"left", "right", "join"}
        )

    def test_descendants_exclude_the_starting_units(self) -> None:
        assert "root" not in self._diamond().descendants(["root"])

    def test_a_leaf_has_no_descendants(self) -> None:
        assert self._diamond().descendants(["join"]) == frozenset()

    def test_ancestors_are_transitive(self) -> None:
        assert self._diamond().ancestors(["join"]) == frozenset(
            {"left", "right", "root"}
        )

    def test_layers_group_parallel_work(self) -> None:
        assert self._diamond().layers() == (("root",), ("left", "right"), ("join",))

    def test_the_widest_layer_bounds_useful_concurrency(self) -> None:
        assert self._diamond().max_width == 2

    def test_layers_over_a_subset_ignore_absent_dependencies(self) -> None:
        """A unit left out because it was up to date is already satisfied."""
        assert self._diamond().layers(["left", "join"]) == (("left",), ("join",))

    def test_an_unknown_unit_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="no build unit named"):
            self._diamond().get("ghost")

    def test_membership(self) -> None:
        graph = self._diamond()
        assert "root" in graph
        assert "ghost" not in graph
        assert len(graph) == 4

    def test_an_empty_graph_has_no_layers(self) -> None:
        assert UnitGraph([]).layers() == ()


class TestDependencyAnalyzer:
    """Inference adds edges; it never removes a declared one."""

    def test_declared_edges_are_kept(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        declared = (unit("core", sources=("core",)), unit("app", sources=("app",), depends_on=("core",)))
        result = run(ProjectAnalyzer().analyze(project, manifest=declared))
        graph = run(DependencyAnalyzer().analyze(result))

        assert graph.dependencies("app") == ("core",)

    def test_imports_become_edges(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        result = run(ProjectAnalyzer().analyze(project))
        graph = run(DependencyAnalyzer().analyze(result))

        assert graph.dependencies("app") == ("core",)
        assert graph.dependencies("core") == ()

    def test_inference_can_be_turned_off(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        result = run(ProjectAnalyzer().analyze(project))
        graph = run(DependencyAnalyzer(infer_imports=False).analyze(result))

        assert graph.dependencies("app") == ()

    def test_third_party_imports_are_not_build_edges(self, project: Path) -> None:
        write_tree(
            project,
            {
                "pyproject.toml": "x",
                "app/__init__.py": "",
                "app/main.py": "import json\nimport requests\n",
            },
        )
        result = run(ProjectAnalyzer().analyze(project))
        graph = run(DependencyAnalyzer().analyze(result))

        assert graph.dependencies("app") == ()

    def test_a_self_import_is_not_an_edge(self, project: Path) -> None:
        write_tree(
            project,
            {"pyproject.toml": "x", "app/__init__.py": "", "app/main.py": "import app\n"},
        )
        result = run(ProjectAnalyzer().analyze(project))
        assert run(DependencyAnalyzer().analyze(result)).dependencies("app") == ()

    def test_unparseable_source_does_not_break_analysis(self, project: Path) -> None:
        """A syntax error is the build's news to break, not the analyzer's."""
        write_tree(
            project,
            {
                "pyproject.toml": "x",
                "app/__init__.py": "",
                "app/broken.py": "def (((\n",
                "core/__init__.py": "",
            },
        )
        result = run(ProjectAnalyzer().analyze(project))
        assert len(run(DependencyAnalyzer().analyze(result))) == 2

    def test_inference_is_skipped_for_other_languages(self, project: Path) -> None:
        write_tree(project, {"Cargo.toml": "x", "src/lib.rs": "fn main() {}"})
        result = run(ProjectAnalyzer().analyze(project))
        assert len(run(DependencyAnalyzer().analyze(result))) == 1

    def test_a_from_import_counts(self, project: Path) -> None:
        write_tree(
            project,
            {
                "pyproject.toml": "x",
                "core/__init__.py": "",
                "app/__init__.py": "",
                "app/main.py": "from core.util import helper\n",
            },
        )
        result = run(ProjectAnalyzer().analyze(project))
        assert run(DependencyAnalyzer().analyze(result)).dependencies("app") == ("core",)

    def test_a_relative_import_is_not_a_cross_unit_edge(self, project: Path) -> None:
        write_tree(
            project,
            {
                "pyproject.toml": "x",
                "core/__init__.py": "",
                "app/__init__.py": "",
                "app/main.py": "from . import sibling\n",
            },
        )
        result = run(ProjectAnalyzer().analyze(project))
        assert run(DependencyAnalyzer().analyze(result)).dependencies("app") == ()

    def test_inferred_cycles_surface_as_cycles(self, project: Path) -> None:
        write_tree(
            project,
            {
                "pyproject.toml": "x",
                "a/__init__.py": "import b\n",
                "b/__init__.py": "import a\n",
            },
        )
        result = run(ProjectAnalyzer().analyze(project))
        with pytest.raises(BuildCycleError):
            run(DependencyAnalyzer().analyze(result))


class TestChangedUnits:
    """Which units a set of edited files implicates."""

    def _layout(self) -> ProjectLayout:
        return layout(
            unit("a", sources=("src/a",)),
            unit("b", sources=("src/b",)),
            sources={"src/a/one.py": "1", "src/b/two.py": "2"},
        )

    def test_a_changed_file_names_its_unit(self) -> None:
        assert changed_units(self._layout(), ["src/a/one.py"]) == frozenset({"a"})

    def test_several_files_name_several_units(self) -> None:
        found = changed_units(self._layout(), ["src/a/one.py", "src/b/two.py"])
        assert found == frozenset({"a", "b"})

    def test_an_unowned_file_names_nobody(self) -> None:
        """Attributing it to an arbitrary unit produces rebuilds that look random."""
        assert changed_units(self._layout(), ["docs/readme.md"]) == frozenset()

    def test_windows_separators_are_accepted(self) -> None:
        assert changed_units(self._layout(), ["src\\a\\one.py"]) == frozenset({"a"})

    def test_no_changes_name_nobody(self) -> None:
        assert changed_units(self._layout(), []) == frozenset()
