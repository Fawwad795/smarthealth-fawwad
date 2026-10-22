"""GET /services/{service_id}/slots through the real FastAPI routing layer.

Every request here is sent without signing in: the route is public, like
the catalogue search a patient uses to find the service in the first place.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Department,
    Provider,
    ProviderService,
    Service,
    Slot,
    Specialty,
    User,
)
from app.models.enums import ServiceStatus, SlotStatus, UserRole

# Fixed and far ahead, so no slot here is ever in the past.
MONDAY_9AM = datetime(2030, 1, 7, 4, 0, tzinfo=UTC)


def _url(service: Service) -> str:
    return f"/api/v1/services/{service.id}/slots"


def _published(db: Session, service: Service) -> Service:
    service.status = ServiceStatus.PUBLISHED
    db.flush()
    return service


def _offer(db: Session, provider: Provider, service: Service) -> None:
    db.add(ProviderService(provider_id=provider.id, service_id=service.id))
    db.flush()


def _slot(
    db: Session,
    provider: Provider,
    starts_at: datetime,
    status: SlotStatus = SlotStatus.AVAILABLE,
) -> Slot:
    """A 30-minute slot of `provider`'s time."""
    slot = Slot(
        provider_id=provider.id,
        start_time=starts_at,
        end_time=starts_at + timedelta(minutes=30),
        status=status,
    )
    db.add(slot)
    db.flush()
    return slot


@pytest.fixture()
def radiologist(db_session: Session, department: Department) -> Provider:
    """A second provider, with a specialty of their own."""
    user = User(
        email="dr.raza@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PROVIDER,
    )
    specialty = Specialty(name="Radiology")
    db_session.add_all([user, specialty])
    db_session.flush()
    provider = Provider(
        user_id=user.id, department_id=department.id, specialty_id=specialty.id
    )
    db_session.add(provider)
    db_session.flush()
    return provider


def test_only_open_future_slots_of_providers_who_offer_the_service_are_listed(
    client: TestClient,
    db_session: Session,
    department: Department,
    service: Service,
    provider: Provider,
    radiologist: Provider,
) -> None:
    """Earliest first, with the ids a booking needs. Reserved, booked,
    blocked and past slots are left out, and so is the radiologist, who
    offers a different service. They must offer one: a provider who
    offers nothing drops out of the join whatever the filter says."""
    other = Service(department_id=department.id, name="Chest X-Ray")
    db_session.add(other)
    db_session.flush()
    _published(db_session, service)
    _offer(db_session, provider, service)
    _offer(db_session, radiologist, other)
    later = _slot(db_session, provider, MONDAY_9AM + timedelta(hours=1))
    earlier = _slot(db_session, provider, MONDAY_9AM)
    for hours, status in [
        (2, SlotStatus.RESERVED),
        (3, SlotStatus.BOOKED),
        (4, SlotStatus.BLOCKED),
    ]:
        _slot(db_session, provider, MONDAY_9AM + timedelta(hours=hours), status)
    _slot(db_session, provider, datetime.now(UTC) - timedelta(days=1))
    _slot(db_session, radiologist, MONDAY_9AM)

    response = client.get(_url(service))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [
        (i["slot_id"], i["provider_id"], i["specialty"]) for i in body["items"]
    ] == [
        (earlier.id, provider.id, "Cardiology"),
        (later.id, provider.id, "Cardiology"),
    ]
    first = body["items"][0]
    assert datetime.fromisoformat(first["start_time"]) == MONDAY_9AM
    assert datetime.fromisoformat(first["end_time"]) == MONDAY_9AM + timedelta(
        minutes=30
    )


@pytest.mark.parametrize(
    "status",
    [
        ServiceStatus.DRAFT,
        ServiceStatus.PUBLISHING,
        ServiceStatus.PUBLISH_FAILED,
        ServiceStatus.INACTIVE,
    ],
)
def test_a_service_that_is_not_published_is_404_like_a_missing_one(
    client: TestClient,
    db_session: Session,
    service: Service,
    provider: Provider,
    status: ServiceStatus,
) -> None:
    """Its open slots stay hidden, and the 404 is the one a missing id
    gets, so the response never confirms the service exists."""
    service.status = status
    _offer(db_session, provider, service)
    _slot(db_session, provider, MONDAY_9AM)

    hidden = client.get(_url(service))
    missing = client.get("/api/v1/services/999999/slots")

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert hidden.json()["error"]["code"] == "SERVICE_NOT_FOUND"


def test_the_list_can_be_narrowed_to_one_provider(
    client: TestClient,
    db_session: Session,
    service: Service,
    provider: Provider,
    radiologist: Provider,
) -> None:
    _published(db_session, service)
    _offer(db_session, provider, service)
    _offer(db_session, radiologist, service)
    _slot(db_session, provider, MONDAY_9AM)
    theirs = _slot(db_session, radiologist, MONDAY_9AM)

    everyone = client.get(_url(service)).json()
    one = client.get(_url(service), params={"provider_id": radiologist.id}).json()

    assert everyone["total"] == 2
    assert one["total"] == 1
    assert one["items"][0]["slot_id"] == theirs.id
    assert one["items"][0]["specialty"] == "Radiology"


def test_the_list_is_paginated_with_the_total_of_every_page(
    client: TestClient,
    db_session: Session,
    service: Service,
    provider: Provider,
) -> None:
    _published(db_session, service)
    _offer(db_session, provider, service)
    slots = [
        _slot(db_session, provider, MONDAY_9AM + timedelta(minutes=30 * n))
        for n in range(3)
    ]

    body = client.get(_url(service), params={"limit": 2, "offset": 1}).json()

    assert body["total"] == 3
    assert (body["limit"], body["offset"]) == (2, 1)
    assert [i["slot_id"] for i in body["items"]] == [s.id for s in slots[1:]]


def test_a_slot_is_listed_under_every_service_its_provider_offers(
    client: TestClient,
    db_session: Session,
    department: Department,
    service: Service,
    provider: Provider,
) -> None:
    """A slot is provider time, chosen for a service only when booked."""
    second = Service(department_id=department.id, name="Stress Test")
    db_session.add(second)
    db_session.flush()
    for each in (service, second):
        _published(db_session, each)
        _offer(db_session, provider, each)
    shared = _slot(db_session, provider, MONDAY_9AM)

    for each in (service, second):
        items = client.get(_url(each)).json()["items"]
        assert [i["slot_id"] for i in items] == [shared.id]


def test_search_by_available_slots_finds_exactly_the_services_with_slots_here(
    client: TestClient,
    db_session: Session,
    department: Department,
    service: Service,
    provider: Provider,
    radiologist: Provider,
) -> None:
    """The two routes share one rule for an open slot, so the catalogue
    never offers a service whose slot list is empty."""
    without = Service(department_id=department.id, name="Chest X-Ray")
    db_session.add(without)
    db_session.flush()
    _published(db_session, service)
    _published(db_session, without)
    _offer(db_session, provider, service)
    _offer(db_session, radiologist, without)
    _slot(db_session, provider, MONDAY_9AM)
    _slot(db_session, radiologist, MONDAY_9AM, SlotStatus.BOOKED)

    found = client.get(
        "/api/v1/services/search", params={"has_available_slots": True}
    ).json()
    with_slots = {
        each.id for each in (service, without) if client.get(_url(each)).json()["total"]
    }

    assert {i["id"] for i in found["items"]} == with_slots == {service.id}
