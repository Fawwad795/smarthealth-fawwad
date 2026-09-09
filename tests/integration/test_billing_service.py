"""Proves BillingChecker.precheck: succeeds by default, fails when the
config switch is flipped, and is idempotent -- a second call for the same
appointment returns the first call's row rather than writing another.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Appointment, Patient, Provider, Service, Slot
from app.models.enums import BillingStatus
from app.services.billing import BillingChecker

BASE = datetime.now(timezone.utc) + timedelta(days=1)


def _appointment(
    db_session: Session,
    provider: Provider,
    patient: Patient,
    service: Service,
    key: str,
) -> Appointment:
    slot = Slot(
        provider_id=provider.id, start_time=BASE, end_time=BASE + timedelta(minutes=30)
    )
    db_session.add(slot)
    db_session.flush()
    appointment = Appointment(
        patient_id=patient.id,
        provider_id=provider.id,
        slot_id=slot.id,
        service_id=service.id,
        idempotency_key=key,
    )
    db_session.add(appointment)
    db_session.flush()
    return appointment


def test_precheck_succeeds_by_default(
    db_session: Session, provider: Provider, patient: Patient, service: Service
) -> None:
    appointment = _appointment(db_session, provider, patient, service, "billing-key-1")

    billing = BillingChecker().precheck(db_session, appointment, "billing-key-1")

    assert billing.status == BillingStatus.CHECKED
    assert billing.appointment_id == appointment.id


def test_precheck_fails_when_forced(
    db_session: Session,
    provider: Provider,
    patient: Patient,
    service: Service,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "billing_force_fail", True)
    appointment = _appointment(db_session, provider, patient, service, "billing-key-2")

    billing = BillingChecker().precheck(db_session, appointment, "billing-key-2")

    assert billing.status == BillingStatus.FAILED


def test_precheck_is_idempotent(
    db_session: Session, provider: Provider, patient: Patient, service: Service
) -> None:
    appointment = _appointment(db_session, provider, patient, service, "billing-key-3")
    checker = BillingChecker()

    first = checker.precheck(db_session, appointment, "billing-key-3")
    second = checker.precheck(db_session, appointment, "billing-key-3")

    assert first.id == second.id
