"""Tests for structured logging, context binding, and secret redaction."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from orchestrator.core.events import AttemptId, RunId, TaskId
from orchestrator.core.logging import (
    REDACTION,
    bind_context,
    configure_logging,
    current_context,
    get_logger,
    register_secret,
    reset_secrets,
)


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    """Install a JSON handler writing to an in-memory stream."""
    buffer = io.StringIO()
    configure_logging(level=logging.DEBUG, stream=buffer, json_output=True)
    yield buffer
    reset_secrets()
    logging.getLogger("orchestrator").handlers.clear()


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    """Parse the captured output into JSON objects."""
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class TestStructuredOutput:
    """Every record is one JSON object with sorted, predictable keys."""

    def test_emits_one_json_object_per_record(self, stream: io.StringIO) -> None:
        get_logger("test").info("hello")
        records = lines(stream)
        assert len(records) == 1
        assert records[0]["message"] == "hello"
        assert records[0]["level"] == "INFO"

    def test_logger_name_is_namespaced(self, stream: io.StringIO) -> None:
        get_logger("task.scheduler").info("x")
        assert lines(stream)[0]["logger"] == "orchestrator.task.scheduler"

    def test_already_namespaced_names_are_not_doubled(self, stream: io.StringIO) -> None:
        get_logger("orchestrator.core.config").info("x")
        assert lines(stream)[0]["logger"] == "orchestrator.core.config"

    def test_structured_fields_become_top_level_keys(self, stream: io.StringIO) -> None:
        get_logger("test").info("attempt finished", status="succeeded", tokens=1234)
        record = lines(stream)[0]
        assert record["status"] == "succeeded"
        assert record["tokens"] == 1234

    def test_keys_are_sorted(self, stream: io.StringIO) -> None:
        get_logger("test").info("x", zebra=1, alpha=2)
        keys = list(lines(stream)[0])
        assert keys == sorted(keys)

    def test_timestamp_is_present_and_utc(self, stream: io.StringIO) -> None:
        get_logger("test").info("x")
        assert str(lines(stream)[0]["ts"]).endswith("+00:00")

    def test_unserializable_values_do_not_break_logging(self, stream: io.StringIO) -> None:
        get_logger("test").info("x", obj=object())
        assert len(lines(stream)) == 1

    def test_level_filtering_is_applied(self) -> None:
        buffer = io.StringIO()
        configure_logging(level=logging.WARNING, stream=buffer, json_output=True)
        log = get_logger("test")
        log.debug("suppressed")
        log.info("suppressed")
        log.warning("kept")
        assert len(lines(buffer)) == 1
        logging.getLogger("orchestrator").handlers.clear()

    def test_exception_includes_a_traceback(self, stream: io.StringIO) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            get_logger("test").exception("failed while working")
        record = lines(stream)[0]
        assert "ValueError: boom" in str(record["exception"])


class TestContextBinding:
    """Identifiers follow the work, and unbind cleanly."""

    def test_no_context_by_default(self, stream: io.StringIO) -> None:
        get_logger("test").info("x")
        assert "run_id" not in lines(stream)[0]
        assert current_context() == {}

    def test_bound_identifiers_appear_on_records(self, stream: io.StringIO) -> None:
        run, task, attempt = RunId.generate(), TaskId.generate(), AttemptId.generate()
        with bind_context(run_id=run, task_id=task, attempt_id=attempt):
            get_logger("test").info("working")
        record = lines(stream)[0]
        assert record["run_id"] == str(run)
        assert record["task_id"] == str(task)
        assert record["attempt_id"] == str(attempt)

    def test_context_is_restored_on_exit(self, stream: io.StringIO) -> None:
        with bind_context(run_id=RunId.generate()):
            pass
        get_logger("test").info("after")
        assert "run_id" not in lines(stream)[0]
        assert current_context() == {}

    def test_nested_binding_adds_without_clobbering(self, stream: io.StringIO) -> None:
        run, task = RunId.generate(), TaskId.generate()
        with bind_context(run_id=run):
            with bind_context(task_id=task):
                get_logger("test").info("inner")
            get_logger("test").info("outer")

        inner, outer = lines(stream)
        assert inner["run_id"] == str(run)
        assert inner["task_id"] == str(task)
        assert outer["run_id"] == str(run)
        assert "task_id" not in outer

    def test_context_is_restored_even_when_the_body_raises(self) -> None:
        with pytest.raises(RuntimeError):  # noqa: SIM117 - nesting is the point
            with bind_context(run_id=RunId.generate()):
                raise RuntimeError("boom")
        assert current_context() == {}


class TestRedaction:
    """Registered secrets never reach the output stream."""

    def test_secret_in_a_message_is_masked(self, stream: io.StringIO) -> None:
        register_secret("super-secret-token-value")
        get_logger("test").info("connecting with super-secret-token-value")
        rendered = stream.getvalue()
        assert "super-secret-token-value" not in rendered
        assert REDACTION in rendered

    def test_secret_in_a_structured_field_is_masked(self, stream: io.StringIO) -> None:
        register_secret("another-secret-value-here")
        get_logger("test").info("x", header="Bearer another-secret-value-here")
        assert "another-secret-value-here" not in stream.getvalue()

    def test_short_values_are_not_registered(self, stream: io.StringIO) -> None:
        register_secret("abc")
        get_logger("test").info("the alphabet starts abc")
        assert "abc" in stream.getvalue()

    def test_none_is_ignored(self) -> None:
        register_secret(None)  # must not raise

    def test_overlapping_secrets_are_fully_masked(self, stream: io.StringIO) -> None:
        register_secret("secret-value-long")
        register_secret("secret-value-longer-still")
        get_logger("test").info("using secret-value-longer-still now")
        rendered = stream.getvalue()
        assert "secret-value-long" not in rendered.replace(REDACTION, "")


class TestConfiguration:
    """Handler installation is idempotent and does not leak into the root."""

    def test_reconfiguring_does_not_duplicate_output(self) -> None:
        buffer = io.StringIO()
        configure_logging(stream=buffer)
        configure_logging(stream=buffer)
        get_logger("test").info("once")
        assert len(lines(buffer)) == 1
        logging.getLogger("orchestrator").handlers.clear()

    def test_does_not_propagate_to_the_root_logger(self) -> None:
        buffer = io.StringIO()
        configure_logging(stream=buffer)
        assert logging.getLogger("orchestrator").propagate is False
        logging.getLogger("orchestrator").handlers.clear()

    def test_text_output_is_available(self) -> None:
        buffer = io.StringIO()
        configure_logging(stream=buffer, json_output=False)
        get_logger("test").info("plain message")
        assert "plain message" in buffer.getvalue()
        with pytest.raises(json.JSONDecodeError):
            json.loads(buffer.getvalue())
        logging.getLogger("orchestrator").handlers.clear()
