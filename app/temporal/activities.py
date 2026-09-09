"""Activities for the service-publishing and appointment-scheduling Workflows.

All I/O for the service publish and scheduling pipeline lives here, never in workflows.py --
Temporal Workflows must be deterministic and cannot touch the database
directly. Bundled on a class rather than left as bare functions so a test
can hand the constructor a factory pointing at the test database instead
of the real one; Activities have no Depends(get_db) to intercept the way
routes do.
"""

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.db.session import SessionLocal
from app.models import (
    Appointment,
    AppointmentStatusHistory,
    ContentChunk,
    ProviderService,
    Service,
    Slot,
    SlotReservation,
)
from app.models.enums import (
    AppointmentStatus,
    BillingStatus,
    ContentSourceType,
    ServiceStatus,
    SlotReservationStatus,
    SlotStatus,
)
from app.services.billing import BillingChecker
from app.services.slot import reserve_slot_uncommitted
from app.workers.tasks.reminders import send_appointment_reminder


@dataclass
class ChunkContentInput:
    """Bundles chunk_content's two arguments into one type.

    Temporal recommends a single object over multiple positional
    arguments: a workflow already running in production can add a field
    to a dataclass without breaking replay of history recorded before the
    field existed, the way inserting a new positional argument would.
    """

    service_id: int
    text: str


class PublishActivities:
    """The four Activities the publish Workflow calls, in order.

    session_factory defaults to the real SessionLocal for production use.
    Tests construct this with a factory that hands back the test's own
    db_session instead, so every method below runs against the test
    database without any of the Activity code knowing the difference.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] = SessionLocal,
    ) -> None:
        """Store the session factory; open nothing until a method runs."""
        self._session_factory = session_factory

    @activity.defn
    def validate_service(self, service_id: int) -> None:
        """Raise a non-retryable error listing every missing field, or
        return None if the service has everything the workflow needs.

        Non-retryable: a missing description is still missing on the next
        attempt, so retrying wastes time instead of fixing anything.
        Temporal's default retry policy would otherwise keep retrying this
        for hours.
        """
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            errors = []
            if not service.description:
                errors.append("description is required")
            if not service.prep_instructions:
                errors.append("prep_instructions is required")
            if errors:
                raise ApplicationError(
                    "; ".join(errors),
                    type="SERVICE_INCOMPLETE",
                    non_retryable=True,
                )

    @activity.defn
    def structure_content(self, service_id: int) -> str:
        """Combine this service's fields into one enriched text block --
        the input the chunk activity splits into content_chunks rows.

        Department name is included so a patient's query ("cardiology
        check-up") can match on more than the bare service name. Provider
        specialty is a documented Week 4 improvement (task 4.3), not added
        here -- a service isn't tied to one single provider.
        """
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            return (
                f"{service.department.name}: {service.name}. "
                f"{service.description} Preparation: {service.prep_instructions}"
            )

    @activity.defn
    def chunk_content(self, input: ChunkContentInput) -> int:
        """Replace this service's content_chunks with one chunk holding
        `input.text`, and return how many chunks were written.

        Delete-then-insert in one transaction, not an incremental diff:
        this is both how re-publishing "replaces chunks atomically" (the
        Definition of Done) and how a retried attempt stays idempotent --
        run once or run twice, the end state is identical either way.
        """
        with self._session_factory() as db:
            db.execute(
                delete(ContentChunk).where(
                    ContentChunk.source_type == ContentSourceType.SERVICE,
                    ContentChunk.source_id == input.service_id,
                )
            )
            db.add(
                ContentChunk(
                    source_type=ContentSourceType.SERVICE,
                    source_id=input.service_id,
                    chunk_index=0,
                    text=input.text,
                    # A real tokenizer arrives with Week 4's embedding step;
                    # this rough estimate is enough for now.
                    token_count=len(input.text) // 4,
                    text_hash=hashlib.sha256(input.text.encode()).hexdigest(),
                )
            )
            db.commit()
            return 1

    @activity.defn
    def mark_published(self, service_id: int) -> None:
        """Transition the service to PUBLISHED -- the workflow's last step."""
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            service.status = ServiceStatus.PUBLISHED
            service.published_at = datetime.now(UTC)
            db.commit()

    @activity.defn
    def mark_publish_failed(self, service_id: int) -> None:
        """Transition the service to PUBLISH_FAILED.

        Called only after validate_service's non-retryable rejection --
        the Workflow's clean-failure path, not something Activities decide
        for themselves.
        """
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            service.status = ServiceStatus.PUBLISH_FAILED
            db.commit()


@dataclass
class RejectInput:
    """Bundles reject's two arguments -- see ChunkContentInput for why."""

    appointment_id: int
    reason: str


@dataclass
class ReleaseSlotInput:
    """Bundles release_slot's two arguments -- see ChunkContentInput for why."""

    appointment_id: int
    reason: str


_ACTOR_SAGA = "SAGA"
_ACTOR_SAGA_COMPENSATION = "SAGA_COMPENSATION"


def _record_transition(
    db: Session,
    appointment: Appointment,
    to_status: AppointmentStatus,
    actor: str,
    reason: str | None = None,
) -> None:
    """Append one AppointmentStatusHistory row and move Appointment.status.

    Every Activity below calls this exactly once per transition, in the
    same commit as its other writes, so the history log and the current
    status column can never drift apart.
    """
    db.add(
        AppointmentStatusHistory(
            appointment_id=appointment.id,
            from_status=appointment.status,
            to_status=to_status,
            actor=actor,
            reason=reason,
        )
    )
    appointment.status = to_status


class SchedulingActivities:
    """The Activities the appointment-scheduling saga Workflow calls.

    Same session_factory injection as PublishActivities, same reason: no
    Depends(get_db) inside a Temporal Activity.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] = SessionLocal,
    ) -> None:
        """Store the session factory; open nothing until a method runs."""
        self._session_factory = session_factory

    @activity.defn
    def validate_eligibility(self, appointment_id: int) -> None:
        """Raise a non-retryable error listing every reason this booking
        can't proceed, or return None if it's eligible.

        Checks the same "published + offered" facts task 1.8's public
        search filters on, plus that the slot actually belongs to the
        provider being booked. None of this can change on a retry, so a
        failure here is non-retryable -- same reasoning as
        validate_service.
        """
        with self._session_factory() as db:
            appointment = db.get(Appointment, appointment_id)
            service = db.get(Service, appointment.service_id)
            slot = db.get(Slot, appointment.slot_id)

            errors = []
            if service.status != ServiceStatus.PUBLISHED:
                errors.append("service is not published")
            offered = db.execute(
                select(ProviderService).where(
                    ProviderService.provider_id == appointment.provider_id,
                    ProviderService.service_id == appointment.service_id,
                )
            ).scalar_one_or_none()
            if offered is None:
                errors.append("provider does not offer this service")
            if slot.provider_id != appointment.provider_id:
                errors.append("slot does not belong to this provider")

            if errors:
                raise ApplicationError(
                    "; ".join(errors),
                    type="APPOINTMENT_INELIGIBLE",
                    non_retryable=True,
                )

    @activity.defn
    def reject(self, input: RejectInput) -> None:
        """Reject a booking that never got as far as reserving a slot.

        Idempotent: REQUESTED is the only legal starting point, so if
        this appointment already moved on, do nothing.
        """
        with self._session_factory() as db:
            appointment = db.get(Appointment, input.appointment_id)
            if appointment.status != AppointmentStatus.REQUESTED:
                return
            _record_transition(
                db, appointment, AppointmentStatus.REJECTED, _ACTOR_SAGA, input.reason
            )
            db.commit()

    @activity.defn
    def reserve_slot(self, appointment_id: int) -> None:
        """Reserve this appointment's slot, or raise if it's already taken.

        Checks for an existing slot_reservations row for this
        (appointment, slot) pair first -- if one exists, this Activity
        already succeeded on a prior attempt, and re-running the atomic
        UPDATE would find the slot already RESERVED by this very
        appointment and wrongly conclude someone else won it. The UPDATE
        and the slot_reservations insert commit together in one
        transaction, so there's no window where the slot is flipped but
        unrecorded.
        """
        with self._session_factory() as db:
            appointment = db.get(Appointment, appointment_id)
            existing = db.execute(
                select(SlotReservation).where(
                    SlotReservation.appointment_id == appointment_id,
                    SlotReservation.slot_id == appointment.slot_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return

            won = reserve_slot_uncommitted(db, appointment.slot_id)
            if not won:
                raise ApplicationError(
                    "slot is no longer available",
                    type="SLOT_UNAVAILABLE",
                    non_retryable=True,
                )

            db.add(
                SlotReservation(
                    appointment_id=appointment_id, slot_id=appointment.slot_id
                )
            )
            _record_transition(
                db, appointment, AppointmentStatus.SLOT_RESERVED, _ACTOR_SAGA
            )
            db.commit()

    @activity.defn
    def billing_precheck(self, appointment_id: int) -> None:
        """Run the simulated billing pre-check, or raise if it fails.

        Idempotent by construction: BillingChecker.precheck() already
        checks for an existing Billing row before creating one (task
        2.8), so a retried call finds -- and reuses -- the first
        attempt's result rather than re-deciding pass/fail from scratch.
        """
        with self._session_factory() as db:
            appointment = db.get(Appointment, appointment_id)
            billing = BillingChecker().precheck(
                db, appointment, appointment.idempotency_key
            )
            if billing.status == BillingStatus.FAILED:
                raise ApplicationError(
                    "billing pre-check failed",
                    type="BILLING_FAILED",
                    non_retryable=True,
                )

    @activity.defn
    def schedule_reminders(self, appointment_id: int) -> None:
        """Queue the Week 3 Celery reminder task for this appointment.

        Fire-and-forget from the saga's point of view: .delay() just drops
        a message on the broker and returns immediately, so this Activity
        finishes in milliseconds regardless of whether celery-worker gets
        to it now or in five minutes. Idempotent by inheritance -- if
        Temporal retries this Activity, the task's own check-before-insert
        (app/services/notification.py) is what actually prevents a second
        reminder, not anything here.
        """
        send_appointment_reminder.delay(appointment_id)

    @activity.defn
    def confirm(self, appointment_id: int) -> None:
        """Confirm the appointment: the saga's last step on the happy path.

        Idempotent: if this appointment is already CONFIRMED, every write
        below is a no-op -- returning immediately avoids re-flipping an
        already-BOOKED slot or writing a duplicate history row.
        """
        with self._session_factory() as db:
            appointment = db.get(Appointment, appointment_id)
            if appointment.status == AppointmentStatus.CONFIRMED:
                return

            slot = db.get(Slot, appointment.slot_id)
            slot.status = SlotStatus.BOOKED

            reservation = db.execute(
                select(SlotReservation).where(
                    SlotReservation.appointment_id == appointment_id,
                    SlotReservation.slot_id == appointment.slot_id,
                )
            ).scalar_one()
            reservation.status = SlotReservationStatus.COMMITTED

            appointment.booked_at = datetime.now(UTC)
            _record_transition(
                db, appointment, AppointmentStatus.CONFIRMED, _ACTOR_SAGA
            )
            db.commit()

    @activity.defn
    def release_slot(self, input: ReleaseSlotInput) -> None:
        """Compensate a reservation: give the slot back after billing fails.

        Idempotent: if this appointment is already CANCELLED, every
        write below is a no-op. This is the saga's own compensating
        Activity -- SAGA_COMPENSATION is reserved for exactly this path,
        not for a patient-requested cancel (task 2.10), even though that
        will reuse similar release logic.
        """
        with self._session_factory() as db:
            appointment = db.get(Appointment, input.appointment_id)
            if appointment.status == AppointmentStatus.CANCELLED:
                return

            slot = db.get(Slot, appointment.slot_id)
            slot.status = SlotStatus.AVAILABLE

            reservation = db.execute(
                select(SlotReservation).where(
                    SlotReservation.appointment_id == input.appointment_id,
                    SlotReservation.slot_id == appointment.slot_id,
                )
            ).scalar_one()
            reservation.status = SlotReservationStatus.RELEASED

            _record_transition(
                db,
                appointment,
                AppointmentStatus.CANCELLED,
                _ACTOR_SAGA_COMPENSATION,
                input.reason,
            )
            db.commit()
