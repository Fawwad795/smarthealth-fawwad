"""Tests for the Celery task layer itself: dead-lettering on permanent
failure, and the periodic rollup task's own entry point.

Both tasks run eagerly (celery_app.conf.task_always_eager, set in
conftest.py) -- .delay() executes inline in this same process. Monkeypatching
each module's own SessionLocal name (not app.db.session's -- each module
already copied its own reference at import time) redirects that inline
execution at db_session instead of the dev database.
"""

from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalyticsDaily, FailedJob
from app.workers.celery_app import celery_app
from app.workers.tasks.analytics import rollup_today
from app.workers.tasks.reminders import send_appointment_reminder


def test_reminder_task_dead_letters_a_permanent_failure(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """A missing appointment is a ValueError, not a transient failure -- it
    must not be autoretried, and must land in failed_jobs instead of just
    vanishing.

    task_eager_propagates (on globally, so every other eager-mode test can
    just pytest.raises) makes Celery skip its own failure handling and
    re-raise directly -- which also skips on_failure, the exact thing this
    test needs to prove ran. Turned off for just this call so on_failure
    actually fires, the one trade only this test needs to make.
    """
    monkeypatch.setattr(
        "app.workers.tasks.reminders.SessionLocal", lambda: nullcontext(db_session)
    )
    monkeypatch.setattr(
        "app.workers.base.SessionLocal", lambda: nullcontext(db_session)
    )
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)

    result = send_appointment_reminder.apply(args=(999999999,))

    assert result.failed()
    failed = db_session.execute(select(FailedJob)).scalar_one()
    assert failed.job_type == "app.workers.tasks.reminders.send_appointment_reminder"
    assert failed.attempts == 1
    assert "999999999" in failed.error


def test_rollup_today_task_writes_todays_row(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(
        "app.workers.tasks.analytics.SessionLocal", lambda: nullcontext(db_session)
    )

    rollup_today.delay()

    row = db_session.get(AnalyticsDaily, datetime.now(UTC).date())
    assert row is not None
