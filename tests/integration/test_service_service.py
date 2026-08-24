"""Service service: create/list/get/update against a real database.

Every service is created DRAFT and stays there -- this module never
touches status or published_at, so there is no lifecycle-transition
assertion here. That belongs to Week 2's publish workflow tests.
"""

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Clinic, Department
from app.models.enums import ServiceStatus
from app.schemas.service import ServiceCreate, ServiceUpdate
from app.services import service as service_service


def _make_department(db_session: Session, clinic: Clinic, name: str) -> Department:
    d = Department(clinic_id=clinic.id, name=name, order_index=0)
    db_session.add(d)
    db_session.flush()
    return d


def test_create_service_starts_as_draft(
    db_session: Session, department: Department
) -> None:
    data = ServiceCreate(
        department_id=department.id,
        name="Knee X-Ray",
        description="Standard knee imaging.",
        prep_instructions="Arrive 15 minutes early.",
    )

    service = service_service.create_service(db_session, data)

    assert service.id is not None
    assert service.status == ServiceStatus.DRAFT
    assert service.published_at is None
    assert service.description == "Standard knee imaging."


def test_duplicate_name_in_same_department_is_rejected(
    db_session: Session, department: Department
) -> None:
    service_service.create_service(
        db_session, ServiceCreate(department_id=department.id, name="Knee X-Ray")
    )

    with pytest.raises(AppError) as exc_info:
        service_service.create_service(
            db_session, ServiceCreate(department_id=department.id, name="knee x-ray")
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "SERVICE_NAME_TAKEN"


def test_same_name_in_a_different_department_is_allowed(
    db_session: Session, clinic: Clinic, department: Department
) -> None:
    other_department = _make_department(db_session, clinic, "Neurology")

    service_service.create_service(
        db_session, ServiceCreate(department_id=department.id, name="Consultation")
    )
    service = service_service.create_service(
        db_session,
        ServiceCreate(department_id=other_department.id, name="Consultation"),
    )

    assert service.department_id == other_department.id


def test_get_service_raises_404_when_missing(db_session: Session) -> None:
    with pytest.raises(AppError) as exc_info:
        service_service.get_service(db_session, 999999999)
    assert exc_info.value.status_code == 404


def test_update_service_renames_it_without_touching_status(
    db_session: Session, department: Department
) -> None:
    service = service_service.create_service(
        db_session, ServiceCreate(department_id=department.id, name="Knee X-Ray")
    )

    updated = service_service.update_service(
        db_session, service.id, ServiceUpdate(name="Knee Imaging")
    )

    assert updated.name == "Knee Imaging"
    assert updated.status == ServiceStatus.DRAFT


def test_update_service_rejects_a_colliding_name(
    db_session: Session, department: Department
) -> None:
    service_service.create_service(
        db_session, ServiceCreate(department_id=department.id, name="Knee X-Ray")
    )
    other = service_service.create_service(
        db_session, ServiceCreate(department_id=department.id, name="Consultation")
    )

    with pytest.raises(AppError) as exc_info:
        service_service.update_service(
            db_session, other.id, ServiceUpdate(name="Knee X-Ray")
        )
    assert exc_info.value.status_code == 409


def test_list_services_paginates(db_session: Session, department: Department) -> None:
    for i in range(3):
        service_service.create_service(
            db_session, ServiceCreate(department_id=department.id, name=f"Service {i}")
        )

    items, total = service_service.list_services(
        db_session, PaginationParams(limit=2, offset=0)
    )
    assert total == 3
    assert len(items) == 2


def test_service_update_schema_has_no_status_field() -> None:
    """Pins down the Step 2 decision: even if a client sends "status" in
    the JSON body, ServiceUpdate has no such field to receive it -- Pydantic
    silently drops unknown keys rather than erroring, so this has to be
    checked on the schema itself, not by sending a request and hoping.
    """
    update = ServiceUpdate.model_validate({"name": "New Name", "status": "PUBLISHED"})
    assert not hasattr(update, "status")
