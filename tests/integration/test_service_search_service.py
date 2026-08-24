"""Public service search: published + offered only, filters as SQL WHERE
clauses. The two things worth pinning down: a DRAFT service never appears
no matter what filter is applied, and each filter (department, specialty,
available slots, name) actually narrows the result set rather than being
decorative.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.pagination import PaginationParams
from app.models import Department, Provider, ProviderService, Service, Slot, Specialty, User
from app.models.enums import ServiceStatus, SlotStatus, UserRole
from app.schemas.service_search import ServiceSearchParams
from app.services import service_search as service_search_service


def _make_service(
    db_session: Session, department: Department, name: str, status: ServiceStatus
) -> Service:
    s = Service(department_id=department.id, name=name, status=status)
    db_session.add(s)
    db_session.flush()
    return s


def _default_params(**overrides) -> ServiceSearchParams:
    base = {
        "q": None,
        "department_id": None,
        "specialty_id": None,
        "has_available_slots": False,
    }
    base.update(overrides)
    return ServiceSearchParams(**base)


def test_search_excludes_draft_services(
    db_session: Session, department: Department
) -> None:
    _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)
    published = _make_service(db_session, department, "MRI Scan", ServiceStatus.PUBLISHED)

    items, total = service_search_service.search_services(
        db_session, _default_params(), PaginationParams()
    )

    assert total == 1
    assert items[0]["id"] == published.id


def test_search_filters_by_name(db_session: Session, department: Department) -> None:
    _make_service(db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED)
    _make_service(db_session, department, "MRI Scan", ServiceStatus.PUBLISHED)

    items, total = service_search_service.search_services(
        db_session, _default_params(q="x-ray"), PaginationParams()
    )

    assert total == 1
    assert items[0]["name"] == "Knee X-Ray"


def test_search_filters_by_department_id(
    db_session: Session, department: Department, clinic
) -> None:
    other_department = Department(clinic_id=clinic.id, name="Neurology", order_index=1)
    db_session.add(other_department)
    db_session.flush()

    _make_service(db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED)
    _make_service(db_session, other_department, "EEG", ServiceStatus.PUBLISHED)

    items, total = service_search_service.search_services(
        db_session, _default_params(department_id=other_department.id), PaginationParams()
    )

    assert total == 1
    assert items[0]["name"] == "EEG"


def test_search_filters_by_specialty_id(
    db_session: Session, department: Department, provider: Provider, specialty: Specialty
) -> None:
    offered = _make_service(db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED)
    _make_service(db_session, department, "MRI Scan", ServiceStatus.PUBLISHED)

    db_session.add(ProviderService(provider_id=provider.id, service_id=offered.id))
    db_session.flush()

    items, total = service_search_service.search_services(
        db_session, _default_params(specialty_id=specialty.id), PaginationParams()
    )

    assert total == 1
    assert items[0]["id"] == offered.id
    assert items[0]["specialties"] == [specialty.name]


def test_search_filters_by_has_available_slots(
    db_session: Session, department: Department, specialty: Specialty, provider: Provider
) -> None:
    """has_available_slots is provider-level, not service-level -- Slot has
    no service_id at all (see slot.py's own docstring: "this is provider
    time"). So proving the filter actually distinguishes two services
    needs two different providers, one with a slot and one without --
    one provider offering both services would make both "have available
    slots" through the same slot, which is correct behaviour, not a bug.
    """
    with_slot = _make_service(db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED)
    without_slot = _make_service(db_session, department, "MRI Scan", ServiceStatus.PUBLISHED)

    second_user = User(
        email="second-provider@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PROVIDER,
    )
    db_session.add(second_user)
    db_session.flush()
    second_provider = Provider(
        user_id=second_user.id, department_id=department.id, specialty_id=specialty.id
    )
    db_session.add(second_provider)
    db_session.flush()

    db_session.add(ProviderService(provider_id=provider.id, service_id=with_slot.id))
    db_session.add(
        ProviderService(provider_id=second_provider.id, service_id=without_slot.id)
    )
    db_session.flush()

    future = datetime.now(timezone.utc) + timedelta(days=1)
    db_session.add(
        Slot(
            provider_id=provider.id,
            start_time=future,
            end_time=future + timedelta(minutes=30),
            status=SlotStatus.AVAILABLE,
        )
    )
    db_session.flush()

    items, total = service_search_service.search_services(
        db_session, _default_params(has_available_slots=True), PaginationParams()
    )

    assert total == 1
    assert items[0]["id"] == with_slot.id


def test_search_includes_department_name(
    db_session: Session, department: Department
) -> None:
    _make_service(db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED)

    items, _ = service_search_service.search_services(
        db_session, _default_params(), PaginationParams()
    )

    assert items[0]["department_name"] == department.name


def test_search_paginates(db_session: Session, department: Department) -> None:
    for i in range(3):
        _make_service(db_session, department, f"Service {i}", ServiceStatus.PUBLISHED)

    items, total = service_search_service.search_services(
        db_session, _default_params(), PaginationParams(limit=2, offset=0)
    )

    assert total == 3
    assert len(items) == 2
