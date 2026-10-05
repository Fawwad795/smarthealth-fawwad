"""Analytics routes: who may read them, and what they return.

Route-level rather than service-level, because the things most likely to
break here are the role check and the query-parameter validation --
neither of which the service layer can see.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.analytics import increment_daily

SUMMARY = "/api/v1/analytics/summary"
SERIES = "/api/v1/analytics/appointments"


@pytest.mark.parametrize("path", [SUMMARY, SERIES])
def test_a_patient_may_not_read_the_analytics(
    client: TestClient, patient_auth_headers: dict[str, str], path: str
) -> None:
    """403, not 404: these are clinic operations figures, and a patient
    being told the route exists is fine -- being shown the numbers is not.
    """
    response = client.get(path, headers=patient_auth_headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("path", [SUMMARY, SERIES])
def test_the_analytics_require_a_token(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_the_summary_reports_the_six_metrics(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Read straight from the aggregates -- no appointment or visit row
    exists in this test, and the numbers still come back.
    """
    day = datetime.now(UTC).date()
    increment_daily(db_session, day, appointments_booked=4, cancellations=1)
    increment_daily(
        db_session, day, completed_visits=2, wait_seconds_total=1200.0, wait_count=2
    )
    db_session.flush()

    body = client.get(SUMMARY, headers=admin_auth_headers).json()

    assert body["appointments_booked"] == 4
    assert body["cancellations"] == 1
    assert body["completed_visits"] == 2
    assert body["cancellation_rate"] == 0.25
    assert body["avg_wait_seconds"] == 600.0
    assert "total_patients" in body
    assert "failed_jobs" in body


def test_the_summary_defaults_to_the_last_thirty_days(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """Both bounds are echoed back, because the server chose them.

    A client that sent neither would otherwise have no way to know which
    days the numbers describe.
    """
    body = client.get(SUMMARY, headers=admin_auth_headers).json()

    end = date.fromisoformat(body["end_date"])
    start = date.fromisoformat(body["start_date"])
    assert end == datetime.now(UTC).date()
    assert (end - start).days == 29  # 30 days inclusive


def test_a_backwards_range_is_rejected(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """422 from the schema, not a hand-rolled check in the router."""
    response = client.get(
        SUMMARY,
        params={"start_date": "2026-06-30", "end_date": "2026-06-01"},
        headers=admin_auth_headers,
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_an_oversized_range_is_rejected(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """The series is not paginated, so the range cap is what bounds the
    response. Without it a caller could ask for a century of buckets.
    """
    response = client.get(
        SERIES,
        params={"start_date": "2020-01-01", "end_date": "2026-01-01"},
        headers=admin_auth_headers,
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_the_series_returns_one_bucket_per_day_in_the_range(
    client: TestClient, front_desk_auth_headers: dict[str, str], db_session: Session
) -> None:
    """Including the quiet days, which have no row at all."""
    day = datetime.now(UTC).date()
    increment_daily(db_session, day, appointments_booked=3)
    db_session.flush()

    start = (day - timedelta(days=2)).isoformat()
    body = client.get(
        SERIES,
        params={"start_date": start, "end_date": day.isoformat()},
        headers=front_desk_auth_headers,
    ).json()

    assert [bucket["appointments_booked"] for bucket in body["buckets"]] == [0, 0, 3]
