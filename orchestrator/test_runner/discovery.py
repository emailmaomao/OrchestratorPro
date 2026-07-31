"""Test discovery — what framework is this, and what tests does it have?

Two distinct questions, both answered here:

**Which framework?** :meth:`TestDiscovery.detect` inspects a directory for
marker files and returns a :class:`Detection` carrying a default command,
parser, and confidence. Nothing is guessed silently: when no framework is
recognized, the result says so rather than defaulting to something plausible,
because a gate that runs the wrong command and exits zero is worse than no gate.

**Which tests?** :meth:`TestDiscovery.collect` asks the framework to enumerate
its own tests without running them (``pytest --collect-only``). That is what
makes it possible to tell "the suite passed" from "the suite was empty" — the
distinction FR-4.4 turns on, and one an exit code alone cannot make.

pytest is supported properly. Other ecosystems are *recognized* — enough to
report what a repository looks like and to refuse confidently — but their
parsers are not implemented, and :attr:`Detection.supported` says so plainly
rather than pretending.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from orchestrator.test_runner.base import TestRunnerError
from orchestrator.test_runner.execution import ProcessRunner, SubprocessRunner

__all__ = ["Detection", "DiscoveryError", "Framework", "TestDiscovery"]


class DiscoveryError(TestRunnerError):
    """A project's test setup could not be determined."""

    code = "test_discovery"
    retryable = False


@dataclass(frozen=True, slots=True)
class Framework:
    """A test framework this system knows how to recognize."""

    name: str
    command: str
    parser: str
    markers: tuple[str, ...]
    supported: bool = False
    config_sections: tuple[tuple[str, ...], ...] = ()


#: Recognized frameworks, most specific first. Only pytest is implemented; the
#: rest exist so a detection can say "this is a Cargo project and I cannot gate
#: it" instead of running something arbitrary.
FRAMEWORKS: Final[tuple[Framework, ...]] = (
    Framework(
        name="pytest",
        command="pytest -q",
        parser="pytest",
        markers=("pytest.ini", "conftest.py", "tox.ini", "setup.cfg", "pyproject.toml"),
        supported=True,
        config_sections=(("tool", "pytest"),),
    ),
    Framework(
        name="cargo",
        command="cargo test",
        parser="exit_code",
        markers=("Cargo.toml",),
    ),
    Framework(
        name="npm",
        command="npm test",
        parser="exit_code",
        markers=("package.json",),
    ),
    Framework(
        name="go",
        command="go test ./...",
        parser="exit_code",
        markers=("go.mod",),
    ),
)

#: Directories that conventionally hold tests.
_TEST_DIRS: Final = ("tests", "test")


@dataclass(frozen=True, slots=True)
class Detection:
    """What discovery concluded about a directory."""

    framework: str
    command: str
    parser: str
    supported: bool
    confidence: str
    evidence: tuple[str, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Whether a gate can actually be run from this detection."""
        return self.supported and self.confidence in ("high", "medium")

    def require_usable(self) -> None:
        """Raise unless this detection can drive a gate.

        Raises:
            DiscoveryError: Explaining what was found and why it is not enough.
        """
        if self.supported and self.usable:
            return
        if not self.supported:
            raise DiscoveryError(
                f"detected a {self.framework} project, which OrchestratorPro cannot "
                "gate yet; configure gates.test_command explicitly",
                detail={"framework": self.framework, "evidence": list(self.evidence)},
            )
        raise DiscoveryError(
            f"could not determine how to run tests (confidence: {self.confidence}); "
            "configure gates.test_command explicitly",
            detail={"evidence": list(self.evidence)},
        )


#: Returned when nothing at all is recognized.
UNKNOWN: Final = Detection(
    framework="unknown",
    command="",
    parser="exit_code",
    supported=False,
    confidence="none",
)


class TestDiscovery:
    """Recognizes a project's test framework and enumerates its tests."""

    __slots__ = ("_runner",)

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        """Bind discovery to a process runner.

        Args:
            runner: Executes collection commands. Defaults to a real subprocess
                runner; tests inject a scripted one.
        """
        self._runner = runner or SubprocessRunner()

    # ------------------------------------------------------------- framework

    def detect(self, cwd: Path) -> Detection:
        """Determine which framework a directory uses.

        Args:
            cwd: The directory to inspect.

        Returns:
            The detection. :data:`UNKNOWN` when nothing is recognized — an
            honest "I do not know" rather than a plausible guess.
        """
        if not cwd.is_dir():
            raise DiscoveryError(
                f"{cwd} is not a directory", detail={"path": str(cwd)}
            )

        for framework in FRAMEWORKS:
            evidence = self._evidence_for(framework, cwd)
            if not evidence:
                continue
            confidence = self._confidence(framework, cwd, evidence)
            if confidence == "none":
                continue
            return Detection(
                framework=framework.name,
                command=framework.command,
                parser=framework.parser,
                supported=framework.supported,
                confidence=confidence,
                evidence=evidence,
            )
        return UNKNOWN

    def _evidence_for(self, framework: Framework, cwd: Path) -> tuple[str, ...]:
        """Return the marker files present for a framework."""
        return tuple(marker for marker in framework.markers if (cwd / marker).exists())

    def _confidence(
        self, framework: Framework, cwd: Path, evidence: Sequence[str]
    ) -> str:
        """Rate how sure the detection is.

        ``pyproject.toml`` alone means little — every Python project has one.
        A pytest configuration section, a ``conftest.py``, or a ``tests``
        directory is what makes it convincing.
        """
        if framework.name != "pytest":
            return "high" if evidence else "none"

        strong = {"pytest.ini", "conftest.py"}
        if strong & set(evidence):
            return "high"
        if self._has_pytest_config(cwd):
            return "high"
        if any((cwd / name).is_dir() for name in _TEST_DIRS):
            return "medium"
        # Only a generic Python marker, with nothing test-shaped beside it.
        return "none"

    def _has_pytest_config(self, cwd: Path) -> bool:
        """Whether ``pyproject.toml`` declares a pytest section."""
        pyproject = cwd / "pyproject.toml"
        if not pyproject.is_file():
            return False
        try:
            with pyproject.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            # A malformed pyproject is not discovery's problem to report; it
            # simply is not evidence.
            return False
        tool = data.get("tool")
        return isinstance(tool, dict) and "pytest" in tool

    def test_directories(self, cwd: Path) -> tuple[Path, ...]:
        """Return the conventional test directories that exist."""
        return tuple(cwd / name for name in _TEST_DIRS if (cwd / name).is_dir())

    # ----------------------------------------------------------- enumeration

    async def collect(
        self, cwd: Path, *, command: str = "pytest --collect-only -q", timeout_s: float = 120.0
    ) -> tuple[str, ...]:
        """Enumerate a suite's tests without running them.

        Args:
            cwd: The directory to collect in.
            command: The collection command.
            timeout_s: Wall-clock limit.

        Returns:
            Test identifiers, in the order the framework reported them.

        Raises:
            DiscoveryError: If collection failed, which almost always means the
                suite cannot be imported — a harness problem, not a test failure.
        """
        result = await self._runner.run(command, cwd=cwd, timeout_s=timeout_s)

        if result.timed_out:
            raise DiscoveryError(
                f"collecting tests timed out after {timeout_s}s",
                detail={"command": command, "timeout_s": timeout_s},
            )
        # pytest exits 5 when it collected nothing. That is an empty suite, not
        # a broken one, so it is reported as an empty tuple rather than raised.
        if result.exit_code not in (0, 5):
            raise DiscoveryError(
                f"collecting tests failed with exit code {result.exit_code}; the "
                "suite could not be imported",
                detail={
                    "command": command,
                    "exit_code": result.exit_code,
                    "stderr": result.stderr[-2000:],
                },
            )

        return tuple(self._parse_collected(result.stdout))

    @staticmethod
    def _parse_collected(stdout: str) -> list[str]:
        """Extract test identifiers from ``pytest --collect-only -q`` output."""
        collected: list[str] = []
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line or "::" not in line:
                continue
            # The trailing summary ("12 tests collected in 0.1s") has no "::".
            if line.startswith(("=", "-", "<", "ERROR", "warning")):
                continue
            collected.append(line)
        return collected

    async def count(self, cwd: Path, **kwargs: object) -> int:
        """Return how many tests a suite contains."""
        return len(await self.collect(cwd, **kwargs))  # type: ignore[arg-type]
