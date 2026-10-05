"""Tests for the correlation-ID middleware, through the real app."""

from fastapi.testclient import TestClient

from app.core.logging import CORRELATION_ID_HEADER


def test_response_carries_a_generated_id(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER].startswith("req-")


def test_a_supplied_id_is_echoed_back_unchanged(client: TestClient) -> None:
    """A client that sends its own id must get that id back, not a new
    one -- otherwise it cannot correlate its own logs with the server's."""
    response = client.get("/health", headers={CORRELATION_ID_HEADER: "req-mine"})

    assert response.headers[CORRELATION_ID_HEADER] == "req-mine"


def test_two_requests_get_different_ids(client: TestClient) -> None:
    first = client.get("/health").headers[CORRELATION_ID_HEADER]
    second = client.get("/health").headers[CORRELATION_ID_HEADER]

    assert first != second


def test_an_error_response_still_carries_the_id(client: TestClient) -> None:
    """The header matters most on a failure -- that is when someone has an
    id to quote. The middleware wraps the exception handlers, so a 404 gets
    one too."""
    response = client.get("/api/v1/no-such-route")

    assert response.status_code == 404
    assert response.headers[CORRELATION_ID_HEADER].startswith("req-")
