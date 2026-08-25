"""Business rules for provider schedule templates and slot generation.

A ProviderSchedule is recurring intent in clinic-local time ("Mondays
09:00-17:00"). generate_slots is what turns that intent into concrete,
bookable Slot rows -- the one piece of code in this project that combines
a bare time, a calendar date and Clinic.timezone into the UTC-aware
timestamps Week 2's atomic reservation depends on.
"""

from datetime import date, datetime, time as time_, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Provider, ProviderSchedule, Slot
from app.schemas.provider_schedule import ProviderScheduleCreate, ProviderScheduleUpdate


def _get_provider_or_404(db: Session, provider_id: int) -> Provider:
    """Resolve the provider_id from the URL path, or raise 404.

    Private to this module: every public function below starts with it, so
    a schedule can never be created for, or listed under, a provider that
    doesn't exist.
    """
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="PROVIDER_NOT_FOUND",
            message="No provider exists with this provider_id.",
        )
    return provider


def create_provider_schedule(
    db: Session, provider_id: int, data: ProviderScheduleCreate
) -> ProviderSchedule:
    """Add one recurring weekly window to a provider's schedule.

    Creates the template only -- no Slot rows exist until generate_slots
    runs. The 409 comes from the (provider_id, weekday, start_time) unique
    constraint: the same window entered twice.
    """
    _get_provider_or_404(db, provider_id)

    schedule = ProviderSchedule(
        provider_id=provider_id,
        weekday=data.weekday,
        start_time=data.start_time,
        end_time=data.end_time,
        slot_duration_minutes=data.slot_duration_minutes,
    )
    db.add(schedule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="DUPLICATE_SCHEDULE_WINDOW",
            message="This provider already has a schedule window starting at this time on this weekday.",
        )
    db.refresh(schedule)
    return schedule


def get_provider_schedule(
    db: Session, provider_id: int, schedule_id: int
) -> ProviderSchedule:
    """Fetch one schedule, checking it really belongs to this provider.

    A schedule_id that exists but belongs to someone else returns the same
    404 as one that doesn't exist at all -- otherwise the difference
    between the two responses would confirm that a given id exists.
    """
    schedule = db.get(ProviderSchedule, schedule_id)
    if schedule is None or schedule.provider_id != provider_id:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SCHEDULE_NOT_FOUND",
            message="No schedule exists with this id for this provider.",
        )
    return schedule


def list_provider_schedules(
    db: Session, provider_id: int, pagination: PaginationParams
) -> tuple[list[ProviderSchedule], int]:
    """Return one page of this provider's schedule windows plus the total.

    Both the count and the page are scoped to provider_id, so the total
    describes this provider's schedules rather than every schedule in the
    system.
    """
    _get_provider_or_404(db, provider_id)

    total = db.execute(
        select(func.count())
        .select_from(ProviderSchedule)
        .where(ProviderSchedule.provider_id == provider_id)
    ).scalar_one()
    items = (
        db.execute(
            select(ProviderSchedule)
            .where(ProviderSchedule.provider_id == provider_id)
            .order_by(ProviderSchedule.weekday, ProviderSchedule.start_time)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        .scalars()
        .all()
    )
    return list(items), total


def update_provider_schedule(
    db: Session, provider_id: int, schedule_id: int, data: ProviderScheduleUpdate
) -> ProviderSchedule:
    """Edit a schedule window, or retire it by setting is_active=False.

    Retiring rather than deleting is deliberate: slots already generated
    from this window keep their provenance, and the row stays available
    for audit.

    Unlike create, the end_time > start_time rule is left to the database's
    CHECK constraint -- a PATCH may send only one of the two, so the schema
    has no way to compare them against the row's existing values.
    """
    schedule = get_provider_schedule(db, provider_id, schedule_id)

    if data.start_time is not None:
        schedule.start_time = data.start_time
    if data.end_time is not None:
        schedule.end_time = data.end_time
    if data.slot_duration_minutes is not None:
        schedule.slot_duration_minutes = data.slot_duration_minutes
    if data.is_active is not None:
        schedule.is_active = data.is_active

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="INVALID_SCHEDULE_WINDOW",
            message="This change violates a schedule constraint -- check end_time is after start_time and this window doesn't duplicate another for this provider/weekday.",
        )
    db.refresh(schedule)
    return schedule


def generate_slots(
    db: Session, provider_id: int, start_date: date, end_date: date
) -> tuple[int, int]:
    """Turn this provider's active schedule templates into concrete Slot
    rows for [start_date, end_date] inclusive, in clinic-local calendar
    days. Idempotent: a start_time that already has a slot is skipped, not
    duplicated, so this is safe to re-run after adding a new template or
    widening the date range.

    Returns (created, skipped).
    """
    provider = _get_provider_or_404(db, provider_id)
    clinic_tz = ZoneInfo(provider.department.clinic.timezone)

    schedules = db.execute(
        select(ProviderSchedule).where(
            ProviderSchedule.provider_id == provider_id,
            ProviderSchedule.is_active.is_(True),
        )
    ).scalars().all()

    range_start_utc = datetime.combine(
        start_date, time_.min, tzinfo=clinic_tz
    ).astimezone(dt_timezone.utc)
    range_end_utc = datetime.combine(
        end_date + timedelta(days=1), time_.min, tzinfo=clinic_tz
    ).astimezone(dt_timezone.utc)

    existing_starts = set(
        db.execute(
            select(Slot.start_time).where(
                Slot.provider_id == provider_id,
                Slot.start_time >= range_start_utc,
                Slot.start_time < range_end_utc,
            )
        ).scalars().all()
    )

    created = 0
    skipped = 0
    new_slots: list[Slot] = []

    current_date = start_date
    while current_date <= end_date:
        weekday = current_date.weekday()
        for schedule in schedules:
            if schedule.weekday != weekday:
                continue

            local_start = datetime.combine(
                current_date, schedule.start_time, tzinfo=clinic_tz
            )
            local_end = datetime.combine(
                current_date, schedule.end_time, tzinfo=clinic_tz
            )
            duration = timedelta(minutes=schedule.slot_duration_minutes)

            slot_start = local_start
            while slot_start + duration <= local_end:
                slot_end = slot_start + duration
                start_utc = slot_start.astimezone(dt_timezone.utc)
                end_utc = slot_end.astimezone(dt_timezone.utc)

                if start_utc in existing_starts:
                    skipped += 1
                else:
                    new_slots.append(
                        Slot(provider_id=provider_id, start_time=start_utc, end_time=end_utc)
                    )
                    existing_starts.add(start_utc)
                    created += 1

                slot_start = slot_end
        current_date += timedelta(days=1)

    db.add_all(new_slots)
    try:
        db.commit()
    except IntegrityError:
        # Two active schedule windows for the same weekday that overlap in
        # time (but don't share an exact start) produce different-looking
        # candidates that still collide in real time. The exclusion
        # constraint on slots is what actually catches that -- see
        # slots.ex_slots_no_overlap.
        db.rollback()
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="OVERLAPPING_SCHEDULE_WINDOWS",
            message="Generating slots for this range would create overlapping slots -- check for overlapping schedule windows on the same weekday.",
        )

    return created, skipped
