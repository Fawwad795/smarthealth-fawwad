"""Appointment routes: request a booking (starts the Week 2 scheduling
saga) and read its current state. Cancel and reschedule land in a later
subtask today.
"""

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.orm import Session
from redis import Redis

from app.core.dependencies import (
    ensure_patient_self_or_staff,
    get_current_user,
    require_role,
)
from app.core.redis import get_redis
from app.db.session import get_db
from app.models import Appointment, User
from app.models.enums import UserRole
from app.schemas.appointment import (
    AppointmentCreate,
    AppointmentReschedule,
    AppointmentResponse,
)
from app.services import appointment_scheduling
from app.services import patient as patient_service

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _response(appointment: Appointment, workflow_id: str) -> AppointmentResponse:
    """Shape one appointment row plus its workflow id into the response.

    workflow_id is derived, not stored, so this cannot be a plain
    model_validate(appointment) -- same reason ServicePublishStatusResponse
    is built field by field.
    """
    return AppointmentResponse(
        id=appointment.id,
        patient_id=appointment.patient_id,
        provider_id=appointment.provider_id,
        slot_id=appointment.slot_id,
        service_id=appointment.service_id,
        status=appointment.status,
        booked_at=appointment.booked_at,
        workflow_id=workflow_id,
    )


@router.post(
    "", response_model=AppointmentResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_appointment(
    data: AppointmentCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    redis_client: Redis = Depends(get_redis),
    current_user: User = Depends(
        require_role(UserRole.PATIENT, UserRole.FRONT_DESK, UserRole.ADMIN)
    ),
) -> AppointmentResponse:
    """Start the appointment-scheduling saga.

    PATIENT books for themselves; FRONT_DESK/ADMIN book on a patient's
    behalf via patient_id. 202: the saga has started, not finished -- poll
    GET /appointments/{id} for the outcome. A repeated Idempotency-Key
    returns the original appointment instead of starting a second saga.
    404 if provider/slot/service/patient doesn't exist, 400 if
    FRONT_DESK/ADMIN omit patient_id.
    """
    patient, actor = patient_service.resolve_acting_patient(
        db, current_user, data.patient_id
    )
    appointment, workflow_id = await appointment_scheduling.request_appointment(
        db, redis_client, data, patient.id, idempotency_key, actor
    )
    return _response(appointment, workflow_id)


@router.get("/{appointment_id}", response_model=AppointmentResponse)
def get_appointment_state(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AppointmentResponse:
    """Report this appointment's current state.

    A patient may only see their own; staff may see any. 404 if it
    doesn't exist.

    Reads status straight off the appointment row rather than querying
    Temporal -- the saga's Activities keep that column in sync at every
    step, the same reasoning GET /services/{id}/publish-status uses.
    """
    appointment = appointment_scheduling.get_appointment(db, appointment_id)
    ensure_patient_self_or_staff(current_user, appointment.patient)
    workflow_id = appointment_scheduling.scheduling_workflow_id(appointment.id)
    return _response(appointment, workflow_id)


@router.post("/{appointment_id}/cancel", response_model=AppointmentResponse)
def cancel_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role(UserRole.PATIENT, UserRole.FRONT_DESK, UserRole.ADMIN)
    ),
) -> AppointmentResponse:
    """Cancel an appointment: release its slot if one was held, and
    promote the next waitlist entry.

    A patient may only cancel their own; staff may cancel any. 404 if it
    doesn't exist, 409 if it's already REJECTED/CANCELLED/COMPLETED.
    """
    appointment = appointment_scheduling.get_appointment(db, appointment_id)
    ensure_patient_self_or_staff(current_user, appointment.patient)
    appointment = appointment_scheduling.cancel_appointment(
        db, appointment_id, current_user.role.value
    )
    workflow_id = appointment_scheduling.scheduling_workflow_id(appointment.id)
    return _response(appointment, workflow_id)


@router.post("/{appointment_id}/reschedule", response_model=AppointmentResponse)
def reschedule_appointment(
    appointment_id: int,
    data: AppointmentReschedule,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role(UserRole.PATIENT, UserRole.FRONT_DESK, UserRole.ADMIN)
    ),
) -> AppointmentResponse:
    """Move an appointment to a different slot with the same provider.

    Releasing the old slot and reserving the new one happen atomically --
    a failed reservation leaves the appointment holding its original
    slot, never neither. A patient may only reschedule their own; staff
    may reschedule any. Status is left unchanged; only the slot moves.
    404 if the appointment or the new slot doesn't exist, 409 if the
    appointment isn't SLOT_RESERVED/CONFIRMED, the new slot belongs to a
    different provider, or the new slot isn't AVAILABLE.
    """
    appointment = appointment_scheduling.get_appointment(db, appointment_id)
    ensure_patient_self_or_staff(current_user, appointment.patient)
    appointment = appointment_scheduling.reschedule_appointment(
        db, appointment_id, data.new_slot_id
    )
    workflow_id = appointment_scheduling.scheduling_workflow_id(appointment.id)
    return _response(appointment, workflow_id)
