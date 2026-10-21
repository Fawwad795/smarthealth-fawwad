"""The AI rate limit (task 5.12): a Redis counter per user per minute,
shared by the five AI routes.

Every test fixes the clock and sets the limit to 2. With the real clock, a
test that ran across a minute boundary would see its count start again and
its 429 never come.
"""

import logging

import pytest
import redis
from fastapi import status
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.embeddings import FakeEmbeddings
from app.ai.retrieval import get_query_embeddings
from app.core.config import settings
from app.core.redis import get_redis
from app.core.security import create_access_token
from app.main import app
from app.models import AIInteraction, User
from app.models.enums import UserRole
from app.services import rate_limit

ASK = "/api/v1/assistant/ask"
ASK_STREAM = "/api/v1/assistant/ask/stream"
REPORT = "/api/v1/reports/generate/utilisation"
QUESTION = {"question": "Which service is right for a knee x-ray?"}

# 1,000,020 is a whole number of minutes, so NOW is 50 seconds into one and
# the next starts in 10.
MINUTE_START = 1_000_020.0
NOW = MINUTE_START + 50


@pytest.fixture(autouse=True)
def _limit_of_two_at_a_fixed_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit of 2 and a fixed clock; questions embedded with FakeEmbeddings.
    The client fixture clears the override when the test ends."""
    monkeypatch.setattr(settings, "ai_rate_limit_per_minute", 2)
    monkeypatch.setattr(rate_limit, "_now", lambda: NOW)
    app.dependency_overrides[get_query_embeddings] = lambda: FakeEmbeddings()


def _limited() -> float:
    """How many requests the limit has refused in this process so far."""
    return REGISTRY.get_sample_value("ai_rate_limited_total") or 0.0


def test_the_request_over_the_limit_is_429_with_retry_after_and_no_ai_work(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict[str, str],
    patient_user: User,
    test_redis_client: redis.Redis,
) -> None:
    """The third request in the minute is refused before any AI work: no
    ai_interactions row, and Retry-After counts to the next minute."""
    before = _limited()

    first, second, third = (
        client.post(ASK, json=QUESTION, headers=patient_auth_headers) for _ in range(3)
    )

    assert (first.status_code, second.status_code) == (200, 200)
    assert third.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert third.json()["error"] == {
        "code": "RATE_LIMITED",
        "message": "Too many AI requests. Try again in 10 seconds.",
    }
    assert third.headers["Retry-After"] == "10"
    assert db_session.scalar(select(func.count()).select_from(AIInteraction)) == 2
    assert _limited() - before == 1
    key = f"ai-rate:{patient_user.id}:{int(NOW // 60)}"
    assert test_redis_client.get(key) == "3"
    assert 0 < test_redis_client.ttl(key) <= 60


def test_the_limit_is_shared_by_the_ask_and_stream_routes(
    client: TestClient, patient_auth_headers: dict[str, str]
) -> None:
    """Switching routes does not reset the count. The stream's 429 is a JSON
    error, since the limit is checked before the stream starts."""
    assert (
        client.post(ASK, json=QUESTION, headers=patient_auth_headers).status_code == 200
    )
    with client.stream(
        "POST", ASK_STREAM, json=QUESTION, headers=patient_auth_headers
    ) as stream:
        assert stream.status_code == 200
        stream.read()

    response = client.post(ASK_STREAM, json=QUESTION, headers=patient_auth_headers)

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_staff_generation_is_limited_too(
    client: TestClient, front_desk_auth_headers: dict[str, str]
) -> None:
    codes = [
        client.post(REPORT, json={}, headers=front_desk_auth_headers).status_code
        for _ in range(3)
    ]

    assert codes == [200, 200, status.HTTP_429_TOO_MANY_REQUESTS]


def test_each_user_has_their_own_count(
    client: TestClient, db_session: Session, patient_auth_headers: dict[str, str]
) -> None:
    other = User(
        email="other.rate.limit@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(other)
    db_session.flush()
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    for _ in range(3):
        client.post(ASK, json=QUESTION, headers=patient_auth_headers)

    response = client.post(ASK, json=QUESTION, headers=other_headers)

    assert response.status_code == 200


def test_a_new_minute_starts_a_new_count(
    client: TestClient,
    patient_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(2):
        client.post(ASK, json=QUESTION, headers=patient_auth_headers)
    assert (
        client.post(ASK, json=QUESTION, headers=patient_auth_headers).status_code == 429
    )

    monkeypatch.setattr(rate_limit, "_now", lambda: MINUTE_START + 60)

    assert (
        client.post(ASK, json=QUESTION, headers=patient_auth_headers).status_code == 200
    )


def test_a_request_refused_for_its_role_is_not_counted(
    client: TestClient,
    front_desk_auth_headers: dict[str, str],
    front_desk_user: User,
    test_redis_client: redis.Redis,
) -> None:
    """The role check runs first, so a forbidden request never reaches the
    counter: 403 every time, and no key for the user."""
    codes = [
        client.post(ASK, json=QUESTION, headers=front_desk_auth_headers).status_code
        for _ in range(3)
    ]

    assert codes == [status.HTTP_403_FORBIDDEN] * 3
    assert not test_redis_client.exists(
        f"ai-rate:{front_desk_user.id}:{int(NOW // 60)}"
    )


def test_a_rejected_question_still_counts(
    client: TestClient, patient_auth_headers: dict[str, str]
) -> None:
    """FastAPI runs the limit, a dependency, before it validates the body,
    so an empty question uses up a request."""
    for _ in range(2):
        response = client.post(ASK, json={"question": ""}, headers=patient_auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    response = client.post(ASK, json=QUESTION, headers=patient_auth_headers)

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS


def test_when_redis_is_down_the_request_is_let_through_logged_and_counted(
    client: TestClient,
    patient_auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three requests over a limit of 2 all get answers. Port 1 on the Redis
    host refuses at once, a real ConnectionError from redis-py."""
    unreachable = redis.Redis(host="redis", port=1, socket_connect_timeout=1)
    app.dependency_overrides[get_redis] = lambda: unreachable
    caplog.set_level(logging.WARNING, logger="app.services.rate_limit")
    before = REGISTRY.get_sample_value("ai_rate_limit_skipped_total") or 0.0

    codes = [
        client.post(ASK, json=QUESTION, headers=patient_auth_headers).status_code
        for _ in range(3)
    ]

    assert codes == [200, 200, 200]
    assert REGISTRY.get_sample_value("ai_rate_limit_skipped_total") - before == 3
    messages = [
        r.getMessage() for r in caplog.records if r.name == "app.services.rate_limit"
    ]
    assert len(messages) == 3
    assert messages[0].endswith("redis unavailable (ConnectionError)")
