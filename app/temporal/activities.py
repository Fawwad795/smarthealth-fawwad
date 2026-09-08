"""Activities for the service-publishing and appointment-scheduling Workflows.

All I/O for the service publish and scheduling pipeline lives here, never in workflows.py --
Temporal Workflows must be deterministic and cannot touch the database
directly. Bundled on a class rather than left as bare functions so a test
can hand the constructor a factory pointing at the test database instead
of the real one; Activities have no Depends(get_db) to intercept the way
routes do.
"""

import hashlib
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core.logging import get_correlation_id, set_correlation_id
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

# Every saga step logs one line, so `grep <correlation-id>` tells a booking's
# whole story across the API, this worker and the Celery worker. Ids only --
# never a patient name, contact detail or free-text reason (rule 6.6).
logger = logging.getLogger(__name__)


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
    correlation_id: str | None = None


@dataclass
class ServiceInput:
    """The service id plus the correlation id, for the publish Activities
    that need nothing else.

    correlation_id defaults to None so a caller outside a request -- a
    script, a test -- can omit it, and so that adding the field did not
    break replay of workflow history recorded before it existed. That
    replay-safety is the same property ChunkContentInput's docstring
    describes, and the reason this is a dataclass and not a second
    positional argument.
    """

    service_id: int
    correlation_id: str | None = None


@dataclass
class AppointmentInput:
    """The appointment id plus the correlation id, shared by the five
    scheduling Activities that need nothing else. See ServiceInput."""

    appointment_id: int
    correlation_id: str | None = None


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
    def validate_service(self, input: ServiceInput) -> None:
        """Raise a non-retryable error listing every missing field, or
        return None if the service has everything the workflow needs.

        Non-retryable: a missing description is still missing on the next
        attempt, so retrying wastes time instead of fixing anything.
        Temporal's default retry policy would otherwise keep retrying this
        for hours.
        """
        set_correlation_id(input.correlation_id)
        service_id = input.service_id
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
    def structure_content(self, input: ServiceInput) -> str:
        """Combine this service's fields into one enriched text block --
        the input the chunk activity splits into content_chunks rows.

        Department name is included so a patient's query ("cardiology
        check-up") can match on more than the bare service name. Provider
        specialty is a documented Week 4 improvement (task 4.3), not added
        here -- a service isn't tied to one single provider.
        """
        set_correlation_id(input.correlation_id)
        service_id = input.service_id
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
        set_correlation_id(input.correlation_id)
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
    def mark_published(self, input: ServiceInput) -> None:
        """Transition the service to PUBLISHED -- the workflow's last step."""
        set_correlation_id(input.correlation_id)
        service_id = input.service_id
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            service.status = ServiceStatus.PUBLISHED
            service.published_at = datetime.now(UTC)
            db.commit()

    @activity.defn
    def mark_publish_failed(self, input: ServiceInput) -> None:
        """Transition the service to PUBLISH_FAILED.

        Called only after validate_service's non-retryable rejection --
        the Workflow's clean-failure path, not something Activities decide
        for themselves.
        """
        set_correlation_id(input.correlation_id)
        service_id = input.service_id
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            service.status = ServiceStatus.PUBLISH_FAILED
            db.commit()


@dataclass
class RejectInput:
    """Bundles reject's two arguments -- see ChunkContentInput for why."""

    appointment_id: int
    reason: str
    correlation_id: str | None = None


@dataclass
class ReleaseSlotInput:
    """Bundles release_slot's two arguments -- see ChunkContentInput for why."""

    appointment_id: int
    reason: str
    correlation_id: str | None = None


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

    Every Activity re-establishes the correlation id from its input before
    doing anything else. The id has to arrive as data because the worker is
    a separate process from the API that started the workflow -- a
    ContextVar crosses no process boundary. Setting it unconditionally also
    stops an Activity inheriting a leftover id from whatever this worker
    thread ran previously.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] = SessionLocal,
    ) -> None:
        """Store the session factory; open nothing until a method runs."""
        self._session_factory = session_factory

    @activity.defn
    def validate_eligibility(self, input: AppointmentInput) -> None:
        """Raise a non-retryable error listing every reason this booking
        can't proceed, or return None if it's eligible.

        Checks the same "published + offered" facts task 1.8's public
        search filters on, plus that the slot actually belongs to the
        provider being booked. None of this can change on a retry, so a
        failure here is non-retryable -- same reasoning as
        validate_service.
        """
        set_correlation_id(input.correlation_id)
        appointment_id = input.appointment_id
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
        set_correlation_id(input.correlation_id)
        with self._session_factory() as db:
            appointment = db.get(Appointment, input.appointment_id)
            if appointment.status != AppointmentStatus.REQUESTED:
                return
            _record_transition(
                db, appointment, AppointmentStatus.REJECTED, _ACTOR_SAGA, input.reason
            )
            db.commit()
        # Logged after the commit, not before: a line claiming a transition
        # that then rolled back is worse than no line at all. The reason text
        # is deliberately omitted -- it is stored on the history row, and
        # keeping free text out of logs is what keeps PHI out of them.
        logger.info("appointment rejected appointment_id=%s", input.appointment_id)

    @activity.defn
    def reserve_slot(self, input: AppointmentInput) -> None:
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
        set_correlation_id(input.correlation_id)
        appointment_id = input.appointment_id
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
            slot_id = appointment.slot_id
        logger.info(
            "slot reserved appointment_id=%s slot_id=%s", appointment_id, slot_id
        )

    @activity.defn
    def billing_precheck(self, input: AppointmentInput) -> None:
        """Run the simulated billing pre-check, or raise if it fails.

        Idempotent by construction: BillingChecker.precheck() already
        checks for an existing Billing row before creating one (task
        2.8), so a retried call finds -- and reuses -- the first
        attempt's result rather than re-deciding pass/fail from scratch.
        """
        set_correlation_id(input.correlation_id)
        appointment_id = input.appointment_id
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
        logger.info("billing pre-check passed appointment_id=%s", appointment_id)

    @activity.defn
    def schedule_reminders(self, input: AppointmentInput) -> None:
        """Queue the Week 3 Celery reminder task for this appointment.

        Fire-and-forget from the saga's point of view: .delay() just drops
        a message on the broker and returns immediately, so this Activity
        finishes in milliseconds regardless of whether celery-worker gets
        to it now or in five minutes. Idempotent by inheritance -- if
        Temporal retries this Activity, the task's own check-before-insert
        (app/services/notification.py) is what actually prevents a second
        reminder, not anything here.

        The correlation id is passed explicitly rather than inherited: the
        broker is a process boundary and carries nothing but the message.
        It is whatever this Activity's own context holds, which is the id
        the workflow was started with -- so the reminder's log lines file
        under the same booking as the request that caused it.
        """
        set_correlation_id(input.correlation_id)
        appointment_id = input.appointment_id
        send_appointment_reminder.delay(
            appointment_id, correlation_id=get_correlation_id()
        )
        logger.info("reminder task queued appointment_id=%s", appointment_id)

    @activity.defn
    def confirm(self, input: AppointmentInput) -> None:
        """Confirm the appointment: the saga's last step on the happy path.

        Idempotent: if this appointment is already CONFIRMED, every write
        below is a no-op -- returning immediately avoids re-flipping an
        already-BOOKED slot or writing a duplicate history row.
        """
        set_correlation_id(input.correlation_id)
        appointment_id = input.appointment_id
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
            slot_id = appointment.slot_id
        logger.info(
            "appointment confirmed appointment_id=%s slot_id=%s",
            appointment_id,
            slot_id,
        )

    @activity.defn
    def release_slot(self, input: ReleaseSlotInput) -> None:
        """Compensate a reservation: give the slot back after billing fails.

        Idempotent: if this appointment is already CANCELLED, every
        write below is a no-op. This is the saga's own compensating
        Activity -- SAGA_COMPENSATION is reserved for exactly this path,
        not for a patient-requested cancel (task 2.10), even though that
        will reuse similar release logic.
        """
        set_correlation_id(input.correlation_id)
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
            slot_id = appointment.slot_id
        # The compensation path is the one a reviewer will want to see in the
        # logs, so it says plainly that this was the saga undoing itself.
        logger.info(
            "slot released by compensation appointment_id=%s slot_id=%s",
            input.appointment_id,
            slot_id,
        )
