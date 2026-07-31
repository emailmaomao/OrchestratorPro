"""Shared fixtures for test_runner tests.

Every test here runs against a **mocked test process**. The process runner is a
seam, so a scripted `ProcessResult` stands in for a real suite: no pytest is
launched inside pytest, nothing depends on a project's actual test layout, and
timeouts are exercised without waiting for one.

A small number of tests write real files (a `coverage.xml`, a `conftest.py`) to
a throwaway directory, because parsing and detection genuinely read the
filesystem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from orchestrator.test_runner.execution import ProcessResult, ScriptedRunner

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


def process(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    *,
    timed_out: bool = False,
    duration_s: float = 0.5,
    command: str = "pytest -q",
) -> ProcessResult:
    """Build a scripted process result."""
    return ProcessResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=duration_s,
        timed_out=timed_out,
        command=command,
    )


def scripted(*results: ProcessResult, default: ProcessResult | None = None) -> ScriptedRunner:
    """Build a runner returning the given results in order."""
    return ScriptedRunner(results, default=default)


@pytest.fixture
def workdir(tmp_dir: Path) -> Path:
    """An existing directory to run gates in."""
    target = tmp_dir / "workspace"
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------------- #
# Representative pytest output
# --------------------------------------------------------------------------- #

PASSING_OUTPUT = """\
============================= test session starts =============================
collected 3 items

tests/test_a.py::test_one PASSED                                        [ 33%]
tests/test_a.py::test_two PASSED                                        [ 66%]
tests/test_b.py::test_three PASSED                                      [100%]

============================== 3 passed in 0.12s ==============================
"""

FAILING_OUTPUT = """\
============================= test session starts =============================
collected 3 items

tests/test_a.py::test_one PASSED                                        [ 33%]
tests/test_a.py::test_two FAILED                                        [ 66%]
tests/test_b.py::test_three PASSED                                      [100%]

=================================== FAILURES ==================================
__________________________________ test_two ___________________________________
    def test_two():
>       assert 1 == 2
E       assert 1 == 2

=========================== short test summary info ===========================
FAILED tests/test_a.py::test_two - assert 1 == 2
========================= 1 failed, 2 passed in 0.34s =========================
"""

COLLECTION_ERROR_OUTPUT = """\
============================= test session starts =============================
==================================== ERRORS ===================================
_____________________ ERROR collecting tests/test_broken.py ___________________
ImportError while importing test module 'tests/test_broken.py'.
E   ModuleNotFoundError: No module named 'nonexistent'
=========================== short test summary info ===========================
ERROR tests/test_broken.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!!
============================== 1 error in 0.08s ===============================
"""

NO_TESTS_OUTPUT = """\
============================= test session starts =============================
collected 0 items

============================ no tests ran in 0.01s ============================
"""

MIXED_OUTPUT = """\
============================= test session starts =============================
tests/test_a.py::test_one PASSED                                        [ 25%]
tests/test_a.py::test_two SKIPPED                                       [ 50%]
tests/test_a.py::test_three XFAIL                                       [ 75%]
tests/test_a.py::test_four FAILED                                       [100%]

=========================== short test summary info ===========================
FAILED tests/test_a.py::test_four - ValueError: boom
============ 1 failed, 1 passed, 1 skipped, 1 xfailed in 0.20s =================
"""

COVERAGE_TERMINAL_OUTPUT = """\
============================= test session starts =============================
tests/test_a.py::test_one PASSED                                        [100%]

---------- coverage: platform win32, python 3.13.5 -----------
Name              Stmts   Miss  Cover
-------------------------------------
src/module.py        80      8    90%
-------------------------------------
TOTAL                80      8    90%

============================== 1 passed in 0.10s ==============================
"""

COVERAGE_XML = """\
<?xml version="1.0" ?>
<coverage line-rate="0.85" branch-rate="0.75" lines-covered="85" lines-valid="100">
  <packages>
    <package name="src">
      <classes>
        <class filename="src/good.py" line-rate="0.95"/>
        <class filename="src/bad.py" line-rate="0.40"/>
      </classes>
    </package>
  </packages>
</coverage>
"""

JUNIT_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" failures="1" errors="0" skipped="0">
    <testcase classname="tests.test_a" name="test_one" time="0.01"/>
    <testcase classname="tests.test_a" name="test_two" time="0.02">
      <failure message="assert 1 == 2">traceback here</failure>
    </testcase>
    <testcase classname="tests.test_b" name="test_three" time="0.03"/>
  </testsuite>
</testsuites>
"""
