"""Proves reserve_slot's atomicity: many concurrent attempts on one slot,
exactly one wins.

Deliberately does not use the db_session fixture. db_session wraps a test
in a transaction that is rolled back at the end and never actually
committed to the database -- so a second, independent connection could
never see the row at all. This test needs many real connections racing
against one row that is genuinely committed, which is exactly the
scenario reserve_slot exists to make safe. It also opens its own engine
sized for the full attempt count rather than reusing the shared `engine`
fixture, whose default pool (5 + 10 overflow) would serialise most of
the 50 attempts through the pool itself before they ever reach Postgres.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Clinic, Department, Provider, Slot, Specialty, User
from app.models.enums import SlotStatus, UserRole
from app.services.slot import reserve_slot

ATTEMPTS = 50


def _committed_slot(stress_engine) -> int:
    """Insert one clinic/provider/slot chain and commit it for real, so
    every thread's own connection can see it. A plain helper, not a
    fixture, because it must use a throwaway session of its own rather
    than db_session's rolled-back one.
    """
    session = Session(bind=stress_engine)
    try:
        clinic = Clinic(name="Concurrency Test Clinic", timezone="UTC")
        specialty = Specialty(name="Concurrency Testing")
        session.add_all([clinic, specialty])
        session.flush()

        department = Department(
            clinic_id=clinic.id, name="Concurrency Dept", order_index=1
        )
        session.add(department)
        session.flush()

        user = User(
            email="concurrency-doctor@example.com",
            password_hash="not-a-real-hash",
            role=UserRole.PROVIDER,
        )
        session.add(user)
        session.flush()

        provider = Provider(
            user_id=user.id,
            department_id=department.id,
            specialty_id=specialty.id,
            bio="Fixture for the concurrency test.",
        )
        session.add(provider)
        session.flush()

        start = datetime.now(timezone.utc) + timedelta(days=1)
        slot = Slot(
            provider_id=provider.id,
            start_time=start,
            end_time=start + timedelta(minutes=30),
        )
        session.add(slot)
        session.commit()
        return slot.id
    finally:
        session.close()


def _attempt_reservation(stress_engine, slot_id: int) -> bool:
    """One thread's attempt: its own session, its own connection."""
    session = Session(bind=stress_engine)
    try:
        return reserve_slot(session, slot_id)
    finally:
        session.close()


def test_concurrent_reservations_exactly_one_wins(test_database: str) -> None:
    stress_engine = create_engine(test_database, pool_size=ATTEMPTS, max_overflow=0)
    try:
        slot_id = _committed_slot(stress_engine)

        with ThreadPoolExecutor(max_workers=ATTEMPTS) as pool:
            results = list(
                pool.map(
                    lambda _: _attempt_reservation(stress_engine, slot_id),
                    range(ATTEMPTS),
                )
            )

        assert results.count(True) == 1
        assert results.count(False) == ATTEMPTS - 1

        verify = Session(bind=stress_engine)
        try:
            reserved_slot = verify.get(Slot, slot_id)
            assert reserved_slot.status == SlotStatus.RESERVED
        finally:
            verify.close()
    finally:
        stress_engine.dispose()
