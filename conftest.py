"""Pytest configuration.

Places the repository root on ``sys.path`` so ``orchestrator`` imports resolve
without an installed distribution. Packaging arrives in M11; until then this is
what makes ``pytest -q`` work from a clean checkout.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def tmp_dir() -> Iterator[Path]:
    """A writable temporary directory, removed on teardown.

    Preferred over pytest's built-in ``tmp_path``. That fixture allocates under
    a shared ``pytest-of-<user>`` root, which is unusable in environments where
    the root already exists with restrictive permissions — a sandbox or CI
    image that inherited it from another account. Allocating our own directory
    per test avoids depending on the state of a shared path we do not own.
    """
    with tempfile.TemporaryDirectory(prefix="orchestratorpro-test-") as name:
        yield Path(name)
