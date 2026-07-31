"""The one place the version number is written down.

There were three of them before v1.0: the distribution metadata, the CLI, and
the API. The first two were checked against each other by a test; the third was
not, so ``/health`` and the OpenAPI document served ``0.1.0`` from a release
tagged ``1.0.0``. A client reading the control plane's version would have been
told it was talking to a pre-release.

Everything that reports a version imports it from here, and a test asserts this
value equals the one in ``pyproject.toml``.
"""

from __future__ import annotations

__all__ = ["VERSION"]

#: The distribution version. Bump here and in ``pyproject.toml`` together.
VERSION = "1.0.0"
