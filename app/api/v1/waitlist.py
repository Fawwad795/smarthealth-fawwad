"""Waitlist routes: join a provider's queue.

Only joining lives here. The queue is *moved* by cancelling an
appointment, not by a request to this router.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.db.session import get_db
from app.models import User
from app.models.enums import UserRole
from app.schemas.errors import error_responses
from app.schemas.waitlist import WaitlistJoin, WaitlistResponse
from app.services import patient as patient_service
from app.services import waitlist as waitlist_service

router = APIRouter(prefix="/waitlist", tags=["waitlist"])


@router.post(
    "",
    response_model=WaitlistResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Join a provider's waitlist",
    responses=error_responses(
        {
            status.HTTP_400_BAD_REQUEST: "Staff joining on a patient's behalf omitted patient_id.",
            status.HTTP_403_FORBIDDEN: "This role may not join a waitlist.",
            status.HTTP_404_NOT_FOUND: "No such provider or patient.",
            status.HTTP_409_CONFLICT: "This patient is already waiting for this provider.",
        }
    ),
)
def join_waitlist(
    data: WaitlistJoin,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role(UserRole.PATIENT, UserRole.FRONT_DESK, UserRole.ADMIN)
    ),
) -> WaitlistResponse:
    """Join a provider's waitlist.

    PATIENT joins for themselves; FRONT_DESK/ADMIN join on a patient's
    behalf via patient_id. 201 rather than 202: this creates a row and
    nothing runs in the background, unlike POST /appointments where there
    is a workflow to accept and poll. 404 if the provider or patient
    doesn't exist, 409 if already waiting, 400 if FRONT_DESK/ADMIN omit
    patient_id.
    """
    patient, _actor = patient_service.resolve_acting_patient(
        db, current_user, data.patient_id
    )
    entry = waitlist_service.join_waitlist(db, data.provider_id, patient.id)
    return WaitlistResponse.model_validate(entry)
