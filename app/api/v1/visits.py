"""Visit routes: check-in through completion for a confirmed appointment.

Nested under /appointments/{appointment_id}/visit because a visit only
exists in the context of one appointment -- the same reasoning that puts
schedules under /providers/{provider_id}.

Driving a visit is staff work: the front desk checks a patient in, the
provider starts and completes. A patient can read their own visit but
never advance it.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import (
    ensure_patient_self_or_staff,
    get_current_user,
    require_role,
)
from app.db.session import get_db
from app.models import User
from app.models.enums import UserRole
from app.schemas.visit import VisitResponse
from app.services import appointment_scheduling, visit as visit_service

router = APIRouter(prefix="/appointments/{appointment_id}/visit", tags=["visits"])

_VISIT_STAFF_ROLES = (UserRole.FRONT_DESK, UserRole.PROVIDER, UserRole.ADMIN)


@router.get("", response_model=VisitResponse)
def get_visit(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VisitResponse:
    """Report where this appointment's visit currently stands.

    A patient may only read their own; staff may read any. 404 if the
    appointment doesn't exist or nobody has been checked in yet.
    """
    appointment = appointment_scheduling.get_appointment(db, appointment_id)
    ensure_patient_self_or_staff(current_user, appointment.patient)
    visit = visit_service.get_visit(db, appointment_id)
    return VisitResponse.model_validate(visit)


@router.post("/check-in", response_model=VisitResponse)
def check_in(
    appointment_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_role(*_VISIT_STAFF_ROLES)),
) -> VisitResponse:
    """Check a patient in, starting their visit. Staff only.

    Idempotent: checking in twice returns the same visit and never
    advances or resets it. 404 if the appointment doesn't exist, 409 if
    it isn't CONFIRMED.
    """
    visit = visit_service.check_in(db, appointment_id)
    return VisitResponse.model_validate(visit)


@router.post("/start", response_model=VisitResponse)
def start_visit(
    appointment_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_role(*_VISIT_STAFF_ROLES)),
) -> VisitResponse:
    """Move the visit to IN_PROGRESS. Staff only.

    Idempotent. 404 if nobody has been checked in, 409 if the visit is
    already COMPLETED.
    """
    visit = visit_service.start_visit(db, appointment_id)
    return VisitResponse.model_validate(visit)


@router.post("/complete", response_model=VisitResponse)
def complete_visit(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_VISIT_STAFF_ROLES)),
) -> VisitResponse:
    """Complete the visit, and the appointment with it. Staff only.

    Idempotent. 404 if nobody has been checked in, 409 if the visit was
    never started.
    """
    visit = visit_service.complete_visit(db, appointment_id, current_user.role.value)
    return VisitResponse.model_validate(visit)
