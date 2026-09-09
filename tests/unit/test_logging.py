"""Tests for the JSON log format and the correlation-ID context."""

import json
import logging

from app.core.logging import (
    CorrelationIdFilter,
    JsonFormatter,
    correlation_id_var,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)


def _record(message: str = "hello", **kwargs: object) -> logging.LogRecord:
    """Build a bare LogRecord the way the logging module would."""
    return logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
        **kwargs,
    )


def test_formatter_emits_the_five_required_fields() -> None:
    """The brief names these fields explicitly; a rename breaks any
    log tooling pointed at them, so they are pinned here."""
    record = _record()
    record.correlation_id = "req-abc123"

    payload = json.loads(JsonFormatter().format(record))

    assert set(payload) == {
        "timestamp",
        "level",
        "logger",
        "correlation_id",
        "message",
    }
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["correlation_id"] == "req-abc123"
    assert payload["message"] == "hello"


def test_formatter_includes_a_traceback_when_there_is_one() -> None:
    """The catch-all handler logs with exc_info; dropping the traceback
    would make the only place a bug is visible useless."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record("failed")
        record.exc_info = logging.sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]


def test_filter_tags_a_record_with_the_current_id() -> None:
    """This is what lets a log call written before any of this existed
    come out carrying an id, with no change at the call site."""
    token = correlation_id_var.set("req-fromcontext")
    try:
        record = _record()
        assert CorrelationIdFilter().filter(record) is True
        assert record.correlation_id == "req-fromcontext"
    finally:
        correlation_id_var.reset(token)


def test_filter_tags_none_outside_any_request() -> None:
    """A Celery worker that has not been handed an id must still log --
    with a null correlation_id, not a crash."""
    record = _record()

    CorrelationIdFilter().filter(record)

    assert record.correlation_id is None


def test_set_correlation_id_mints_one_when_given_nothing() -> None:
    token = correlation_id_var.set(None)
    try:
        minted = set_correlation_id(None)
        assert minted.startswith("req-")
        assert get_correlation_id() == minted
    finally:
        correlation_id_var.reset(token)


def test_set_correlation_id_keeps_a_supplied_id() -> None:
    """A caller's own id must survive untouched -- that is what makes one
    id span the client's retry as well as the original attempt."""
    token = correlation_id_var.set(None)
    try:
        assert set_correlation_id("req-supplied") == "req-supplied"
        assert get_correlation_id() == "req-supplied"
    finally:
        correlation_id_var.reset(token)


def test_minted_ids_are_unique() -> None:
    assert new_correlation_id() != new_correlation_id()
