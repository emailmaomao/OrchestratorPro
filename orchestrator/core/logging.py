"""Structured logging with run context binding and secret redaction.

Every log line is a JSON object carrying the identifiers of the run, task, and
attempt that produced it. Context is held in :mod:`contextvars`, so it follows
work across ``await`` boundaries and stays correct when several attempts run
concurrently on one event loop — the case that breaks thread-local approaches.

Two rules from ``CLAUDE.md`` are enforced structurally rather than by review:

* **No ``print``.** All output goes through a logger, so it can be routed,
  filtered, and captured.
* **No secrets in logs** (NFR-3.3). Values registered with :func:`register_secret`
  are masked in rendered output, including inside nested structured fields.

Redaction is a backstop, not a licence. A secret that never reaches a log line
cannot leak; this exists for the case where one slips into an exception message
built by code we do not own.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import IO, Any, Final

from orchestrator.core.events import AttemptId, RunId, TaskId

__all__ = [
    "BoundLogger",
    "JsonFormatter",
    "bind_context",
    "configure_logging",
    "current_context",
    "get_logger",
    "register_secret",
    "reset_secrets",
]

#: Placeholder substituted for any registered secret value.
REDACTION: Final = "***REDACTED***"

#: Minimum length for a registered secret. Masking a one-character value would
#: replace every occurrence of that character across all output.
_MIN_SECRET_LENGTH: Final = 8

_LOGGER_NAMESPACE: Final = "orchestrator"

#: Record attributes set by :mod:`logging` itself, excluded from structured
#: fields so that user-supplied keys are the only extras rendered.
_STANDARD_ATTRS: Final = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)

_run_id: ContextVar[RunId | None] = ContextVar("orchestrator_run_id", default=None)
_task_id: ContextVar[TaskId | None] = ContextVar("orchestrator_task_id", default=None)
_attempt_id: ContextVar[AttemptId | None] = ContextVar("orchestrator_attempt_id", default=None)
#: Free-form correlation values — a request identifier, a build identifier.
#: Kept separate from the typed identifiers above so that binding one cannot
#: be mistaken for binding a run.
_extra: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "orchestrator_extra_context", default=()
)

_secrets: set[str] = set()


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #


def register_secret(value: str | None) -> None:
    """Register a value to be masked wherever it appears in log output.

    Short and empty values are ignored: masking them would corrupt unrelated
    output far more often than it would protect anything.

    Args:
        value: The secret to mask. ``None`` and short values are no-ops.
    """
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _secrets.add(value)


def reset_secrets() -> None:
    """Forget all registered secrets. Intended for test isolation."""
    _secrets.clear()


def _redact(text: str) -> str:
    """Mask every registered secret occurring in ``text``."""
    if not _secrets:
        return text
    # Longest first, so an overlapping shorter secret cannot leave a fragment.
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTION)
    return text


# --------------------------------------------------------------------------- #
# Context binding
# --------------------------------------------------------------------------- #


def current_context() -> dict[str, str]:
    """Return the identifiers currently bound, omitting those unset."""
    context: dict[str, str] = {}
    if (run := _run_id.get()) is not None:
        context["run_id"] = str(run)
    if (task := _task_id.get()) is not None:
        context["task_id"] = str(task)
    if (attempt := _attempt_id.get()) is not None:
        context["attempt_id"] = str(attempt)
    context.update(dict(_extra.get()))
    return context


@contextmanager
def bind_context(
    *,
    run_id: RunId | None = None,
    task_id: TaskId | None = None,
    attempt_id: AttemptId | None = None,
    **extra: str,
) -> Iterator[None]:
    """Bind identifiers to every log record emitted inside the block.

    Bindings nest: an inner block adds to the outer one, and only the values it
    set are unbound on exit. Restoration happens even if the body raises, so a
    failing attempt cannot leak its identifiers into later work.

    Args:
        run_id: Run to bind, or ``None`` to leave the current binding intact.
        task_id: Task to bind, or ``None``.
        attempt_id: Attempt to bind, or ``None``.
        **extra: Free-form correlation values, such as a request identifier.
            Each is stringified and attached to every record in the block.

    Yields:
        ``None``. The bindings are active for the duration of the block.
    """
    tokens: list[tuple[ContextVar[Any], Token[Any]]] = []
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if task_id is not None:
        tokens.append((_task_id, _task_id.set(task_id)))
    if attempt_id is not None:
        tokens.append((_attempt_id, _attempt_id.set(attempt_id)))
    if extra:
        merged = {**dict(_extra.get()), **{k: str(v) for k, v in extra.items()}}
        tokens.append((_extra, _extra.set(tuple(merged.items()))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class _ContextFilter(logging.Filter):
    """Attach the currently bound identifiers to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_context().items():
            setattr(record, key, value)
        return True


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


class JsonFormatter(logging.Formatter):
    """Render a log record as one deterministic JSON object per line.

    Keys are sorted so that output diffs cleanly and can be compared across
    runs. Values that cannot be serialized are coerced with :func:`repr` rather
    than raising — a logging failure must never mask the event being logged.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a single JSON line."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        rendered = json.dumps(payload, sort_keys=True, default=repr)
        return _redact(rendered)


# --------------------------------------------------------------------------- #
# Logger facade
# --------------------------------------------------------------------------- #


class BoundLogger:
    """A thin facade over :class:`logging.Logger` taking structured fields.

    Keyword arguments become top-level keys in the emitted JSON object, so
    downstream tooling can filter on them without parsing message text::

        log.info("attempt finished", status="succeeded", tokens=12_345)
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @property
    def name(self) -> str:
        """The underlying logger's dotted name."""
        return self._logger.name

    def _log(self, level: int, message: str, fields: Mapping[str, Any]) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, message, extra=dict(fields))

    def debug(self, message: str, **fields: Any) -> None:
        """Log at DEBUG with structured fields."""
        self._log(logging.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        """Log at INFO with structured fields."""
        self._log(logging.INFO, message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        """Log at WARNING with structured fields."""
        self._log(logging.WARNING, message, fields)

    def error(self, message: str, **fields: Any) -> None:
        """Log at ERROR with structured fields."""
        self._log(logging.ERROR, message, fields)

    def exception(self, message: str, **fields: Any) -> None:
        """Log at ERROR including the active exception's traceback."""
        if self._logger.isEnabledFor(logging.ERROR):
            self._logger.error(message, exc_info=True, extra=dict(fields))


def get_logger(name: str) -> BoundLogger:
    """Return a logger under the ``orchestrator`` namespace.

    Args:
        name: A module name, typically ``__name__``. Names already inside the
            namespace are used unchanged.

    Returns:
        A :class:`BoundLogger` wrapping the underlying logger.
    """
    if name == _LOGGER_NAMESPACE or name.startswith(f"{_LOGGER_NAMESPACE}."):
        full_name = name
    else:
        full_name = f"{_LOGGER_NAMESPACE}.{name}"
    return BoundLogger(logging.getLogger(full_name))


def configure_logging(
    *,
    level: int | str = logging.INFO,
    stream: IO[str] | None = None,
    json_output: bool = True,
) -> logging.Handler:
    """Install a single handler on the ``orchestrator`` logger.

    Idempotent: calling it again replaces the previous handler rather than
    stacking a second one, which would double every line.

    Args:
        level: Minimum level to emit.
        stream: Destination. Defaults to :data:`sys.stderr` so that structured
            logs never contaminate a program's stdout.
        json_output: Emit JSON when true, human-readable text when false.

    Returns:
        The installed handler, so tests can inspect or remove it.
    """
    logger = logging.getLogger(_LOGGER_NAMESPACE)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    handler.addFilter(_ContextFilter())

    logger.addHandler(handler)
    logger.setLevel(level)
    # The namespace owns its output; propagating would duplicate every line
    # into whatever handler the embedding application installed on the root.
    logger.propagate = False
    return handler
