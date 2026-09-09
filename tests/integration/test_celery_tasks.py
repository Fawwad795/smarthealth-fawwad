"""Tests for the Celery task layer itself: dead-lettering on permanent
failure, and each periodic task's own entry point.

Both tasks run eagerly (celery_app.conf.task_always_eager, set in
conftest.py) -- .delay() executes inline in this same process. The
worker_session fixture redirects that inline execution at db_session
instead of the dev database, by patching the single SessionLocal lookup
that session_scope() makes at call time.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import correlation_id_var, get_correlation_id
from app.events import relay
from app.events.envelope import EventType
from app.events.outbox import record_event
from app.models import AnalyticsDaily, FailedJob, OutboxEvent
from app.workers.celery_app import celery_app
from app.workers.tasks.analytics import rollup_today
from app.workers.tasks.events import publish_outbox_events
from app.workers.tasks.reminders import send_appointment_reminder


def test_reminder_task_dead_letters_a_permanent_failure(
    monkeypatch: pytest.MonkeyPatch, worker_session: None, db_session: Session
) -> None:
    """A missing appointment is a ValueError, not a transient failure -- it
    must not be autoretried, and must land in failed_jobs instead of just
    vanishing.

    Two different modules open a session on this path -- the task body and
    DeadLetterTask.on_failure -- and both used to need patching separately.
    They now share session_scope(), so worker_session covers both.

    task_eager_propagates (on globally, so every other eager-mode test can
    just pytest.raises) makes Celery skip its own failure handling and
    re-raise directly -- which also skips on_failure, the exact thing this
    test needs to prove ran. Turned off for just this call so on_failure
    actually fires, the one trade only this test needs to make.
    """
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)

    result = send_appointment_reminder.apply(args=(999999999,))

    assert result.failed()
    failed = db_session.execute(select(FailedJob)).scalar_one()
    assert failed.job_type == "app.workers.tasks.reminders.send_appointment_reminder"
    assert failed.attempts == 1
    assert "999999999" in failed.error


def test_rollup_today_task_writes_todays_row(
    worker_session: None, db_session: Session
) -> None:
    rollup_today.delay()

    row = db_session.get(AnalyticsDaily, datetime.now(UTC).date())
    assert row is not None


def _spy_on_reminder(monkeypatch: pytest.MonkeyPatch, seen: list[str | None]) -> None:
    """Replace the reminder service with a spy that records the correlation
    id visible inside the task, which is the thing under test here -- not
    whether a notification row was written."""

    def spy(db: Session, appointment_id: int) -> None:
        seen.append(get_correlation_id())

    monkeypatch.setattr(
        "app.workers.tasks.reminders.notification_service.send_appointment_reminder",
        spy,
    )


def test_reminder_task_reestablishes_a_supplied_correlation_id(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """The id the API minted must be the id the task logs under."""
    seen: list[str | None] = []
    _spy_on_reminder(monkeypatch, seen)

    send_appointment_reminder.delay(1, correlation_id="req-from-api")

    assert seen == ["req-from-api"]


def test_reminder_task_mints_an_id_when_given_none(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """A task queued without an id still needs one -- a null correlation_id
    on every line is not traceable."""
    token = correlation_id_var.set(None)
    try:
        seen: list[str | None] = []
        _spy_on_reminder(monkeypatch, seen)

        send_appointment_reminder.delay(1)

        assert seen[0] is not None
        assert seen[0].startswith("req-")
    finally:
        correlation_id_var.reset(token)


def test_a_task_does_not_inherit_the_previous_tasks_id(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """The bug this guards is subtle and would be believed if it happened:
    a prefork worker runs tasks back to back in one process, so a task that
    did not set the ContextVar would file its logs under the *previous*
    booking's id. Two runs in one process, second one given no id."""
    seen: list[str | None] = []
    _spy_on_reminder(monkeypatch, seen)

    send_appointment_reminder.delay(1, correlation_id="req-first")
    send_appointment_reminder.delay(2)

    assert seen[0] == "req-first"
    assert seen[1] != "req-first"


def test_rollup_task_mints_its_own_id(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """Beat has no upstream request, so each scheduled run identifies
    itself rather than logging under null."""
    seen: list[str | None] = []

    def spy(db: Session, day: object) -> None:
        seen.append(get_correlation_id())

    monkeypatch.setattr(
        "app.workers.tasks.analytics.analytics_service.rollup_analytics_for_date", spy
    )
    token = correlation_id_var.set("req-leftover-from-something-else")
    try:
        rollup_today.delay()
        assert seen[0] is not None
        assert seen[0] != "req-leftover-from-something-else"
    finally:
        correlation_id_var.reset(token)


def test_outbox_relay_task_publishes_what_is_waiting(
    monkeypatch: pytest.MonkeyPatch, worker_session: None, db_session: Session
) -> None:
    """The Beat-scheduled entry point, exercised end to end bar the broker.

    relay.publish is replaced rather than the relay itself, so the task's
    own wiring -- opening a session, delegating, committing -- is what runs
    here. Testing it by mocking publish_pending_events would prove only
    that the task calls a function.
    """
    monkeypatch.setattr(relay, "publish", lambda topic, key, value: None)
    record_event(db_session, EventType.APPOINTMENT_BOOKED, 1, {"appointment_id": 1})
    db_session.commit()

    publish_outbox_events.delay()

    pending = (
        db_session.execute(
            select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
        )
        .scalars()
        .all()
    )
    assert pending == []


def test_outbox_relay_task_mints_its_own_correlation_id(
    monkeypatch: pytest.MonkeyPatch, worker_session: None, db_session: Session
) -> None:
    """Beat has no upstream request, so the relay run identifies itself.

    This is not the id the events carry -- each envelope already holds the
    correlation id of whatever request created it. This one only ties the
    relay's own log lines together for one run.
    """
    seen: list[str | None] = []

    def spy(db: Session, limit: int = relay.BATCH_SIZE) -> int:
        seen.append(get_correlation_id())
        return 0

    monkeypatch.setattr(relay, "publish_pending_events", spy)
    token = correlation_id_var.set("req-left-over")
    try:
        publish_outbox_events.delay()
        assert seen[0] is not None
        assert seen[0] != "req-left-over"
    finally:
        correlation_id_var.reset(token)
