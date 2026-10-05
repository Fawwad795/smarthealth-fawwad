"""The /metrics page, and the labelling rule that keeps it safe.

The interesting test here is not that the page renders -- it is that a URL
containing an id becomes one metric rather than one metric per id.
"""

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.core.metrics import METRICS_PATH


def _requests_counted(endpoint: str, method: str = "GET", status: str = "200") -> float:
    """How many requests this process has recorded for one label set.

    Counters are module-level and shared by every test in the run, so a
    test must compare before and after rather than expect a fixed number.
    """
    value = REGISTRY.get_sample_value(
        "http_requests_total",
        {"method": method, "endpoint": endpoint, "status": status},
    )
    return value or 0.0


def test_metrics_page_exposes_real_http_samples(client: TestClient) -> None:
    """The page carries actual measurements, not just declarations.

    Asserted on a labelled sample line rather than a name, because a
    Counter that has never been incremented still prints its # HELP and
    # TYPE headers -- so searching the page for a bare name proves only
    that somebody declared it.
    """
    client.get("/health")

    body = client.get(METRICS_PATH).text

    assert 'http_requests_total{endpoint="/health",method="GET",status="200"}' in body
    assert "http_request_duration_seconds_bucket" in body


def test_metrics_page_declares_the_domain_counters(client: TestClient) -> None:
    """Every counter the brief names is registered and spelled correctly.

    Declaration is genuinely all this can check. Importing app.core.metrics
    registers all four counters in whatever process did the importing, and
    the test process imports everything -- so their *values* here mean
    nothing. In the running system each number lives on the page of the
    process that witnesses it: the Temporal worker sees slot collisions,
    the consumer sees events, and the API sees neither. Prometheus keeps
    them apart by its own `job` label, so a query wanting the system-wide
    figure sums across jobs.
    """
    body = client.get(METRICS_PATH).text

    for name in (
        "appointments_booked_total",
        "double_booking_prevented_total",
        "events_consumed_total",
        "events_failed_total",
    ):
        assert name in body


def test_requests_are_labelled_by_route_not_by_url(client: TestClient) -> None:
    """Two ids on one route produce one metric, not two.

    The guard against unbounded label growth -- the failure mode that
    exhausts a Prometheus server's memory. If this ever fails, someone has
    started labelling with request.url.path.
    """
    before = _requests_counted("/api/v1/appointments/{appointment_id}", status="401")

    client.get("/api/v1/appointments/1")
    client.get("/api/v1/appointments/2")

    after = _requests_counted("/api/v1/appointments/{appointment_id}", status="401")
    assert after - before == 2

    body = client.get(METRICS_PATH).text
    assert "/api/v1/appointments/1" not in body
    assert "/api/v1/appointments/2" not in body


def test_unmatched_paths_share_one_label(client: TestClient) -> None:
    """A 404 is counted under "unmatched", never under the URL requested.

    Otherwise anyone could mint unlimited labels by requesting nonsense.
    """
    before = _requests_counted("unmatched", status="404")

    client.get("/no-such-route-abc")
    client.get("/no-such-route-xyz")

    assert _requests_counted("unmatched", status="404") - before == 2
    assert "no-such-route" not in client.get(METRICS_PATH).text


def test_metrics_endpoint_does_not_count_itself(client: TestClient) -> None:
    """Scrapes are excluded, or they would swamp the real traffic."""
    before = _requests_counted(METRICS_PATH)

    client.get(METRICS_PATH)

    assert _requests_counted(METRICS_PATH) == before
