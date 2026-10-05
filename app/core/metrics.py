"""The numbers each process publishes for Prometheus to scrape.

A log line says "this happened, here are the details". A metric says "this
has now happened N times" -- a single number, kept in memory, that a
monitoring tool reads on a schedule. Logs answer "what went wrong with this
one booking". Metrics answer "is anything going wrong right now".

Everything here is per *process*. A Counter is an ordinary Python object
living in one program's memory, so the API, the Temporal worker and the
Kafka consumer each keep their own separate tallies and each publish their
own page. That is why prometheus.yml scrapes three targets instead of one:
the Temporal worker is the only process that ever sees a slot collision, so
it is the only one that could possibly count them.
"""

import time
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
    start_http_server,
)

# The path the API serves its page on. Prometheus is configured with this
# same string, so it lives in one place rather than two.
METRICS_PATH = "/metrics"

# Ports the two non-web processes publish on. Kept side by side because
# prometheus.yml has to name the same numbers, and a mismatch shows up only
# as a target that is quietly DOWN -- never as an error anywhere.
WORKER_METRICS_PORT = 8001  # temporal worker
CONSUMER_METRICS_PORT = 8002

# --- HTTP metrics --------------------------------------------------------
#
# prometheus_client appends "_total" to a Counter's name for you, so
# "http_requests" below is published as "http_requests_total". Writing the
# suffix in yourself is the usual first mistake -- you end up asking for
# http_requests_total_total.

http_requests = Counter(
    "http_requests",
    "HTTP requests handled, by endpoint and outcome.",
    ["method", "endpoint", "status"],
)

http_request_duration = Histogram(
    "http_request_duration_seconds",
    "How long each request took, in seconds.",
    ["method", "endpoint"],
)


# --- Domain counters -----------------------------------------------------
#
# Each is incremented by the one process that actually witnesses the thing.

appointments_booked = Counter(
    "appointments_booked",
    "Appointments the scheduling saga carried all the way to CONFIRMED.",
)

double_booking_prevented = Counter(
    "double_booking_prevented",
    "Times a slot reservation lost the race and was refused.",
)

events_consumed = Counter(
    "events_consumed",
    "Events the consumer took off a topic and finished with.",
    ["event_type", "outcome"],
)

events_failed = Counter(
    "events_failed",
    "Messages the consumer could not process.",
    ["reason"],
)


def endpoint_label(request: Any) -> str:
    """The route *template* for a request, e.g. /api/v1/appointments/{id}.

    This matters far more than it looks. Labelling with the real path would
    make a separate, permanent metric for /appointments/1, /appointments/2
    and every other id anyone ever asks for. Prometheus holds each one in
    memory forever, and that is the standard way people take their own
    monitoring server down with their own instrumentation. The template
    collapses every id into a single entry.

    A request that matched no route has no template at all, so it gets one
    fixed label rather than whatever URL was typed. Without that, anyone
    could invent unlimited new labels just by requesting nonsense paths.
    """
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


def metrics_payload() -> tuple[bytes, str]:
    """This process's whole page, plus the content type it must be sent as.

    Prometheus expects its own plain-text format, not JSON -- so this is the
    one place in the app that deliberately ignores our response schemas.
    """
    return generate_latest(), CONTENT_TYPE_LATEST


def start_metrics_server(port: int) -> None:
    """Give a non-web process somewhere for Prometheus to reach it.

    The API already runs a web server, so it just adds a route. The Temporal
    worker and the Kafka consumer are not web servers at all -- this starts
    a background thread whose only job is to serve the page on `port`.
    """
    start_http_server(port)


def observe_request(method: str, endpoint: str, status: str, started: float) -> None:
    """Record one finished request: how long it took, and how it ended.

    Both numbers are updated together so they can never disagree about how
    many requests happened. `started` is a time.perf_counter() reading taken
    before the request ran.
    """
    http_request_duration.labels(method, endpoint).observe(
        time.perf_counter() - started
    )
    http_requests.labels(method, endpoint, status).inc()
