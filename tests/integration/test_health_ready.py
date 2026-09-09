"""/health/ready -- and specifically, that it can fail.

A readiness check that only ever returns 200 is decoration. These tests
break one dependency at a time and insist the endpoint notices.
"""

from fastapi.testclient import TestClient

from app.core import health


def test_ready_reports_every_dependency(client: TestClient) -> None:
    """With the stack up, all four are named and all four are ok."""
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {
        "database": "ok",
        "redis": "ok",
        "kafka": "ok",
        "temporal": "ok",
    }


def test_ready_returns_503_when_a_dependency_is_down(
    client: TestClient, monkeypatch
) -> None:
    """One broken dependency fails the endpoint but not the others.

    Patched as health.check_kafka rather than an imported name: run_checks
    looks the function up on its module when it calls it, which is the only
    reason replacing it here works at all.
    """

    def refuse() -> bool:
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(health, "check_kafka", refuse)

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["kafka"] == "down"
    # The other three still report honestly -- one failure must not
    # blank out the rest of the picture.
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


def test_ready_does_not_leak_connection_details(
    client: TestClient, monkeypatch
) -> None:
    """A driver's error text can carry the host and password it dialled.

    Whatever the exception says stays in the log; the response says "down".
    """

    def refuse() -> bool:
        raise ConnectionError("could not connect to redis://app:hunter2@redis:6379/0")

    monkeypatch.setattr(health, "check_redis", lambda client: refuse())

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert "hunter2" not in response.text
    assert "6379" not in response.text
    assert response.json()["checks"]["redis"] == "down"
