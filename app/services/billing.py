"""Simulated billing for the appointment scheduling saga.

Real payment/insurance integration is out of scope (CLAUDE.md #4) -- this
exists purely so task 2.9's saga has a real billing step to run, one that
can genuinely succeed or fail, so its compensation path (releasing the
slot on failure) has something real to react to.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Appointment, Billing
from app.models.enums import BillingStatus

# No pricing model exists anywhere in the domain (Service and
# ProviderService carry no price). A fixed amount is enough to exercise
# CHECKED/FAILED and the compensation path -- real pricing is out of scope.
_PLACEHOLDER_AMOUNT = Decimal("100.00")


class BillingChecker:
    """One method: pre-check billing for an appointment, simulated."""

    def precheck(
        self, db: Session, appointment: Appointment, idempotency_key: str
    ) -> Billing:
        """Record this appointment's billing pre-check, or return the one
        already recorded.

        Checked-before-insert, the same convention as everywhere else in
        this codebase: an Activity can be retried at any time, and a
        retry must find the first attempt's row rather than write a
        second one for the same appointment.
        """
        existing = db.execute(
            select(Billing).where(Billing.appointment_id == appointment.id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        status = (
            BillingStatus.FAILED
            if settings.billing_force_fail
            else BillingStatus.CHECKED
        )
        billing = Billing(
            appointment_id=appointment.id,
            amount=_PLACEHOLDER_AMOUNT,
            status=status,
            idempotency_key=idempotency_key,
        )
        db.add(billing)
        db.commit()
        return billing
