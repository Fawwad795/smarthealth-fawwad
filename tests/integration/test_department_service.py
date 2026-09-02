"""Department service: create/list/get/update against a real database.

Departments have no workflow -- just rows that must stay valid. The
behaviour worth pinning down is the per-clinic name uniqueness (case
insensitive, checked before the insert) and the 404/409 failure shapes.
"""

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Clinic
from app.schemas.department import DepartmentCreate, DepartmentUpdate
from app.services import department as department_service


def _make_clinic(db_session: Session, name: str) -> Clinic:
    c = Clinic(name=name, timezone="Asia/Karachi")
    db_session.add(c)
    db_session.flush()
    return c


def test_create_department_persists_it(db_session: Session, clinic: Clinic) -> None:
    data = DepartmentCreate(clinic_id=clinic.id, name="Orthopaedics", order_index=2)

    department = department_service.create_department(db_session, data)

    assert department.id is not None
    assert department.name == "Orthopaedics"
    assert department.order_index == 2


def test_duplicate_name_in_same_clinic_is_rejected(
    db_session: Session, clinic: Clinic
) -> None:
    department_service.create_department(
        db_session, DepartmentCreate(clinic_id=clinic.id, name="Cardiology")
    )

    with pytest.raises(AppError) as exc_info:
        department_service.create_department(
            db_session, DepartmentCreate(clinic_id=clinic.id, name="cardiology")
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "DEPARTMENT_NAME_TAKEN"


def test_same_name_in_a_different_clinic_is_allowed(
    db_session: Session, clinic: Clinic
) -> None:
    other_clinic = _make_clinic(db_session, "MediNova North")

    department_service.create_department(
        db_session, DepartmentCreate(clinic_id=clinic.id, name="Cardiology")
    )
    department = department_service.create_department(
        db_session, DepartmentCreate(clinic_id=other_clinic.id, name="Cardiology")
    )

    assert department.clinic_id == other_clinic.id


def test_get_department_raises_404_when_missing(db_session: Session) -> None:
    with pytest.raises(AppError) as exc_info:
        department_service.get_department(db_session, 999999999)
    assert exc_info.value.status_code == 404


def test_update_department_renames_it(db_session: Session, clinic: Clinic) -> None:
    department = department_service.create_department(
        db_session, DepartmentCreate(clinic_id=clinic.id, name="Cardiology")
    )

    updated = department_service.update_department(
        db_session, department.id, DepartmentUpdate(name="Cardiac Sciences")
    )

    assert updated.name == "Cardiac Sciences"


def test_update_department_rejects_a_colliding_name(
    db_session: Session, clinic: Clinic
) -> None:
    department_service.create_department(
        db_session, DepartmentCreate(clinic_id=clinic.id, name="Cardiology")
    )
    other = department_service.create_department(
        db_session, DepartmentCreate(clinic_id=clinic.id, name="Neurology")
    )

    with pytest.raises(AppError) as exc_info:
        department_service.update_department(
            db_session, other.id, DepartmentUpdate(name="Cardiology")
        )
    assert exc_info.value.status_code == 409


def test_list_departments_paginates(db_session: Session, clinic: Clinic) -> None:
    for i in range(3):
        department_service.create_department(
            db_session,
            DepartmentCreate(clinic_id=clinic.id, name=f"Dept {i}", order_index=i),
        )

    items, total = department_service.list_departments(
        db_session, PaginationParams(limit=2, offset=0)
    )
    assert total == 3
    assert len(items) == 2

    items_page_2, total_page_2 = department_service.list_departments(
        db_session, PaginationParams(limit=2, offset=2)
    )
    assert len(items_page_2) == 1
    assert total_page_2 == 3
