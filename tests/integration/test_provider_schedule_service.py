"""ProviderSchedule CRUD and generate_slots against a real database.

The behaviour worth pinning down: generate_slots correctly converts a bare
clinic-local time into a UTC-aware Slot.start_time, is idempotent on
re-run, skips inactive schedules, and turns a genuine overlap between two
schedule windows into a clean 409 instead of a raw database error.
"""

from datetime import date, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Provider, Slot
from app.schemas.provider_schedule import ProviderScheduleCreate, ProviderScheduleUpdate
from app.services import provider_schedule as schedule_service

# Provider's clinic fixture is "Asia/Karachi" -- UTC+5, no DST -- so every
# expected UTC time below is exactly 5 hours behind the local schedule time.
_UTC_OFFSET = timedelta(hours=5)


def test_create_provider_schedule_persists_it(
    db_session: Session, provider: Provider
) -> None:
    data = ProviderScheduleCreate(
        weekday=0, start_time="09:00", end_time="17:00", slot_duration_minutes=30
    )

    schedule = schedule_service.create_provider_schedule(db_session, provider.id, data)

    assert schedule.id is not None
    assert schedule.slot_duration_minutes == 30
    assert schedule.is_active is True


def test_create_schedule_rejects_a_missing_provider(db_session: Session) -> None:
    data = ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")

    with pytest.raises(AppError) as exc_info:
        schedule_service.create_provider_schedule(db_session, 999999999, data)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "PROVIDER_NOT_FOUND"


def test_create_schedule_rejects_a_duplicate_window(
    db_session: Session, provider: Provider
) -> None:
    data = ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")
    schedule_service.create_provider_schedule(db_session, provider.id, data)

    with pytest.raises(AppError) as exc_info:
        schedule_service.create_provider_schedule(db_session, provider.id, data)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "DUPLICATE_SCHEDULE_WINDOW"


def test_get_schedule_raises_404_for_a_different_providers_schedule(
    db_session: Session, provider: Provider
) -> None:
    schedule = schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00"),
    )

    with pytest.raises(AppError) as exc_info:
        schedule_service.get_provider_schedule(db_session, 999999999, schedule.id)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "SCHEDULE_NOT_FOUND"


def test_update_schedule_deactivates_it(
    db_session: Session, provider: Provider
) -> None:
    schedule = schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00"),
    )

    updated = schedule_service.update_provider_schedule(
        db_session, provider.id, schedule.id, ProviderScheduleUpdate(is_active=False)
    )

    assert updated.is_active is False


def test_list_schedules_paginates(db_session: Session, provider: Provider) -> None:
    for weekday in range(3):
        schedule_service.create_provider_schedule(
            db_session,
            provider.id,
            ProviderScheduleCreate(weekday=weekday, start_time="09:00", end_time="17:00"),
        )

    items, total = schedule_service.list_provider_schedules(
        db_session, provider.id, PaginationParams(limit=2, offset=0)
    )
    assert total == 3
    assert len(items) == 2


def test_generate_slots_produces_correct_utc_timestamps(
    db_session: Session, provider: Provider
) -> None:
    monday = date(2027, 3, 1)
    while monday.weekday() != 0:
        monday += timedelta(days=1)

    schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(
            weekday=0, start_time="09:00", end_time="11:00", slot_duration_minutes=30
        ),
    )

    created, skipped = schedule_service.generate_slots(
        db_session, provider.id, monday, monday
    )

    assert created == 4
    assert skipped == 0

    slots = (
        db_session.execute(
            select(Slot).where(Slot.provider_id == provider.id).order_by(Slot.start_time)
        )
        .scalars()
        .all()
    )
    assert len(slots) == 4
    expected_first_start = (
        __import__("datetime")
        .datetime.combine(monday, __import__("datetime").time(9, 0))
        .replace(tzinfo=timezone.utc)
        - _UTC_OFFSET
    )
    assert slots[0].start_time.astimezone(timezone.utc) == expected_first_start
    assert slots[0].start_time.tzinfo is not None


def test_generate_slots_is_idempotent_on_rerun(
    db_session: Session, provider: Provider
) -> None:
    monday = date(2027, 3, 1)
    while monday.weekday() != 0:
        monday += timedelta(days=1)

    schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(
            weekday=0, start_time="09:00", end_time="10:00", slot_duration_minutes=30
        ),
    )

    first_created, first_skipped = schedule_service.generate_slots(
        db_session, provider.id, monday, monday
    )
    second_created, second_skipped = schedule_service.generate_slots(
        db_session, provider.id, monday, monday
    )

    assert first_created == 2
    assert first_skipped == 0
    assert second_created == 0
    assert second_skipped == 2

    total_slots = db_session.execute(
        select(Slot).where(Slot.provider_id == provider.id)
    ).scalars().all()
    assert len(total_slots) == 2


def test_generate_slots_skips_inactive_schedules(
    db_session: Session, provider: Provider
) -> None:
    monday = date(2027, 3, 1)
    while monday.weekday() != 0:
        monday += timedelta(days=1)

    schedule = schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="10:00"),
    )
    schedule_service.update_provider_schedule(
        db_session, provider.id, schedule.id, ProviderScheduleUpdate(is_active=False)
    )

    created, skipped = schedule_service.generate_slots(
        db_session, provider.id, monday, monday
    )

    assert created == 0
    assert skipped == 0


def test_generate_slots_rejects_a_missing_provider(db_session: Session) -> None:
    with pytest.raises(AppError) as exc_info:
        schedule_service.generate_slots(db_session, 999999999, date.today(), date.today())
    assert exc_info.value.status_code == 404


def test_generate_slots_raises_409_on_a_genuine_overlap(
    db_session: Session, provider: Provider
) -> None:
    monday = date(2027, 3, 1)
    while monday.weekday() != 0:
        monday += timedelta(days=1)

    # 45-minute slots from 09:00 produce a slot ending 09:45. 30-minute
    # slots from 09:15 produce one starting at 09:15 -- a different
    # start_time (so forcing it onto weekday 0 doesn't itself collide with
    # the first schedule's own (provider_id, weekday, start_time) row) that
    # still overlaps the first schedule's 09:00-09:45 slot in real time.
    schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(
            weekday=0, start_time="09:00", end_time="10:00", slot_duration_minutes=45
        ),
    )
    schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(
            weekday=1, start_time="09:15", end_time="10:00", slot_duration_minutes=30
        ),
    )
    # Force the second schedule onto the same weekday as the first --
    # ProviderScheduleCreate itself would reject a duplicate (provider_id,
    # weekday, start_time), but these two differ on start_time, so this
    # direct update doesn't collide with that constraint either.
    second = db_session.execute(
        select(schedule_service.ProviderSchedule).where(
            schedule_service.ProviderSchedule.provider_id == provider.id,
            schedule_service.ProviderSchedule.slot_duration_minutes == 30,
        )
    ).scalar_one()
    second.weekday = 0
    db_session.commit()

    with pytest.raises(AppError) as exc_info:
        schedule_service.generate_slots(db_session, provider.id, monday, monday)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "OVERLAPPING_SCHEDULE_WINDOWS"
