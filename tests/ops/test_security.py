"""The security posture, checked mechanically instead of by memory.

`SECURITY.md` makes three claims that decay silently if nothing watches them:
that bandit's rules are part of the standing lint, that every waiver carries a
reason, and that no invariant rests on an `assert`. Each is cheap to verify from
the source tree, so each is verified here rather than trusted.

The dependency audit is the one check that needs the network, so it is marked
`live` and stays out of the default suite — the same rule every other network
test follows.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "orchestrator"


def _pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data: dict[str, object] = tomllib.load(handle)
    return data


def _lint_config() -> dict[str, object]:
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    ruff = tool["ruff"]
    assert isinstance(ruff, dict)
    lint = ruff["lint"]
    assert isinstance(lint, dict)
    return lint


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


class TestBanditIsPartOfTheStandingLint:
    """`S` must be selected globally, not run as a separate pass someone forgets."""

    def test_the_S_rules_are_selected(self) -> None:
        select = _lint_config()["select"]
        assert isinstance(select, list)
        assert "S" in select, "flake8-bandit dropped out of the ruff selection"

    def test_the_test_waivers_do_not_leak_into_the_package(self) -> None:
        ignores = _lint_config()["per-file-ignores"]
        assert isinstance(ignores, dict)
        for pattern, rules in ignores.items():
            assert isinstance(rules, list)
            waived = [rule for rule in rules if str(rule).startswith("S")]
            if not waived:
                continue
            assert pattern.startswith("tests"), (
                f"{pattern!r} waives {waived} outside tests/; bandit waivers "
                "belong on the line that needs them, with a reason"
            )


class TestEveryWaiverExplainsItself:
    """A bare `# noqa: S...` is an unreviewed finding wearing a suppression."""

    def test_each_noqa_carries_a_reason(self) -> None:
        bare: list[str] = []
        for path in _sources():
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                marker = line.find("# noqa: S")
                if marker == -1:
                    continue
                # A reason is anything past the rule codes — "S603 - argv
                # only" — or, when it does not fit in 96 columns, a comment
                # on the line above. Both are reviewable; a bare code is not.
                tail = line[marker + len("# noqa: ") :]
                codes, _, inline = tail.partition(" ")
                del codes
                above = lines[index - 1].strip() if index else ""
                if not inline.strip(" -") and not above.startswith("#"):
                    bare.append(f"{path.relative_to(REPO_ROOT)}:{index + 1}")
        assert not bare, (
            "these suppressions give no reason; SECURITY.md promises every one "
            f"is reviewed: {bare}"
        )


class TestNoInvariantRestsOnAnAssert:
    """`python -O` strips `assert`, so a guarantee written as one is not one."""

    def test_the_package_contains_no_executable_assert(self) -> None:
        offenders: list[str] = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                    )
        assert not offenders, (
            "asserts vanish under python -O; raise, or bind locally to narrow "
            f"for the type checker: {offenders}"
        )


@pytest.mark.live
class TestDependenciesHaveNoKnownVulnerabilities:
    """The audit SECURITY.md points at. Needs PyPI's advisory database."""

    def test_pip_audit_reports_nothing(self) -> None:
        import os

        if not os.environ.get("ORCHESTRATORPRO_TEST_PIP_AUDIT"):
            pytest.skip("set ORCHESTRATORPRO_TEST_PIP_AUDIT=1 to run")
        pytest.importorskip("pip_audit", reason="pip-audit is a dev dependency")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "-r",
                str(REPO_ROOT / "requirements.txt"),
                "--progress-spinner",
                "off",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert completed.returncode == 0, (
            f"pip-audit found something:\n{completed.stdout}\n{completed.stderr}"
        )
