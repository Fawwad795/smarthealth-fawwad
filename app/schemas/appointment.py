"""Appointment request/response schemas.

No status field on AppointmentCreate: booking runs through the Week 2
scheduling saga (task 2.9), the same reasoning ServiceCreate uses to keep
status off its own writable schema.
"""

from datetime import datetime

from pydantic import BaseModel

from app.models.enums import AppointmentStatus


class AppointmentCreate(BaseModel):
    """Everything needed to request an appointment.

    patient_id is optional here because a PATIENT books for themselves --
    the router derives it from the authenticated user. Only FRONT_DESK/
    ADMIN, booking on someone else's behalf, need to set it.
    """

    provider_id: int
    slot_id: int
    service_id: int
    patient_id: int | None = None


class AppointmentReschedule(BaseModel):
    """A request to move an appointment to a different slot.

    Same provider only -- moving to a different provider is a new
    booking, not a reschedule.
    """

    new_slot_id: int


class AppointmentResponse(BaseModel):
    """One appointment's current state, including the saga's workflow id
    so a caller can look it up directly in the Temporal UI if needed.
    """

    id: int
    patient_id: int
    provider_id: int
    slot_id: int
    service_id: int
    status: AppointmentStatus
    booked_at: datetime | None
    workflow_id: str
