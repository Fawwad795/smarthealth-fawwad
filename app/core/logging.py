"""Structured JSON logging and the per-request correlation ID.

Every process in this system -- the API, the Temporal worker, the Celery
worker, and from Day 3 the Kafka consumer -- calls configure_logging() at
startup, so all four emit the same JSON shape. One booking's lines can then
be pulled out of all of them with a single grep on its correlation ID.

Never log PHI. Log ids -- appointment_id, patient_id, slot_id. Names,
contact details and free text stay out of log output entirely (rule 6.6).
"""

import json
import logging
import uuid
from contextvars import ContextVar

from app.core.config import settings

# The header a caller may send to supply their own id, and the one we always
# send back. Defined here rather than in main.py because the tests and, from
# Day 3, the consumer all need the same string.
CORRELATION_ID_HEADER = "X-Request-ID"

# The current request's correlation ID.
#
# A ContextVar rather than a plain module global because many requests are in
# flight at once: asyncio gives each its own view of this one variable, where
# a global would be overwritten by whichever request wrote last. It is empty
# in a freshly started process, which is exactly why work handed to another
# process has to carry the id inside the message.
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    """Mint an id for a request that arrived without one."""
    return f"req-{uuid.uuid4().hex[:12]}"


def set_correlation_id(correlation_id: str | None) -> str:
    """Put an id into this process's context, minting one if none was given.

    Called by the API middleware for an incoming request, and by everything
    on the far side of a process boundary -- a Celery task, a Temporal
    Activity, the Kafka consumer -- to re-establish the id that travelled
    with the message. Returns the id actually used.
    """
    resolved = correlation_id or new_correlation_id()
    correlation_id_var.set(resolved)
    return resolved


def get_correlation_id() -> str | None:
    """The current context's id, or None outside any request or task."""
    return correlation_id_var.get()


def ensure_correlation_id() -> str:
    """The current context's id, minting and storing one if there is none.

    Used at a process boundary, where an id has to be handed to another
    process but the caller might not be inside a request -- a script, a
    test, or a scheduled job. Unlike set_correlation_id() it never
    replaces an id that is already there.
    """
    return set_correlation_id(get_correlation_id())


class CorrelationIdFilter(logging.Filter):
    """Staples the current correlation ID onto every log record.

    A logging Filter is allowed to modify a record rather than only accept
    or reject it. That is what lets logger.info("reminder sent"), written
    before any of this existed, come out carrying an id with no change at
    the call site.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Always returns True -- this filter tags, it never drops."""
        record.correlation_id = get_correlation_id()
        return True


class JsonFormatter(logging.Formatter):
    """Renders one log record as a single line of JSON.

    Hand-written rather than pulled in from structlog: it is fifteen lines,
    it keeps the dependency list short, and the fields are the ones the
    brief asks for rather than whatever a library defaults to.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Build the JSON line. Field names are fixed -- tools parse these."""
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", None),
            "message": record.getMessage(),
        }
        # A traceback is the one thing worth more than a single line: without
        # it, the catch-all handler's log is unactionable.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    """Point the root logger at one JSON handler. Called once, at startup.

    Replaces whatever handlers are already installed. uvicorn and Celery
    each configure their own on import, and leaving those attached prints
    every line twice -- once as JSON through root, once as the library's
    own prose.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # uvicorn attaches handlers to these three itself. Clearing them and
    # letting the records propagate up to root is what stops access logs
    # appearing in a second, unparseable format alongside ours.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
