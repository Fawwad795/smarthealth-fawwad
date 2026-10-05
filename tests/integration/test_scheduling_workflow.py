"""Tests for AppointmentSchedulingWorkflow's orchestration logic: which
Activity runs, in what order, and which compensating Activity fires on
each of the three expected-failure types. Runs against
WorkflowEnvironment's time-skipping test server with fake Activities
that record call order instead of touching a database -- the real
Activities' database behaviour is already proven by
test_scheduling_activities.py. Same pattern as test_publish_workflow.py.
"""

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from app.temporal.activities import AppointmentInput, RejectInput, ReleaseSlotInput
from app.temporal.workflows import AppointmentSchedulingWorkflow
from tests.temporal_env import start_test_env


_TASK_QUEUE = "test_scheduling-workflow"


class _RecordingActivities:
    """Fakes for the saga's seven Activities: same names (so Temporal
    resolves them the same way it would resolve SchedulingActivities'
    real methods), but they record call order instead of touching a
    database, and can be told to fail at exactly one step.
    """

    def __init__(
        self,
        fail_eligibility: bool = False,
        eligibility_error_type: str = "APPOINTMENT_INELIGIBLE",
        fail_reserve: bool = False,
        reserve_error_type: str = "SLOT_UNAVAILABLE",
        fail_billing: bool = False,
        billing_error_type: str = "BILLING_FAILED",
    ) -> None:
        self.calls: list[str] = []
        self.correlation_ids: list[str | None] = []
        self._fail_eligibility = fail_eligibility
        self._eligibility_error_type = eligibility_error_type
        self._fail_reserve = fail_reserve
        self._reserve_error_type = reserve_error_type
        self._fail_billing = fail_billing
        self._billing_error_type = billing_error_type

    @activity.defn
    async def validate_eligibility(self, input: AppointmentInput) -> None:
        self.calls.append("validate_eligibility")
        self.correlation_ids.append(input.correlation_id)
        if self._fail_eligibility:
            raise ApplicationError(
                "service is not published",
                type=self._eligibility_error_type,
                non_retryable=True,
            )

    @activity.defn
    async def reject(self, input: RejectInput) -> None:
        self.calls.append("reject")
        self.correlation_ids.append(input.correlation_id)

    @activity.defn
    async def reserve_slot(self, input: AppointmentInput) -> None:
        self.calls.append("reserve_slot")
        self.correlation_ids.append(input.correlation_id)
        if self._fail_reserve:
            raise ApplicationError(
                "slot is no longer available",
                type=self._reserve_error_type,
                non_retryable=True,
            )

    @activity.defn
    async def billing_precheck(self, input: AppointmentInput) -> None:
        self.calls.append("billing_precheck")
        self.correlation_ids.append(input.correlation_id)
        if self._fail_billing:
            raise ApplicationError(
                "billing pre-check failed",
                type=self._billing_error_type,
                non_retryable=True,
            )

    @activity.defn
    async def schedule_reminders(self, input: AppointmentInput) -> None:
        self.calls.append("schedule_reminders")
        self.correlation_ids.append(input.correlation_id)

    @activity.defn
    async def confirm(self, input: AppointmentInput) -> None:
        self.calls.append("confirm")
        self.correlation_ids.append(input.correlation_id)

    @activity.defn
    async def release_slot(self, input: ReleaseSlotInput) -> None:
        self.calls.append("release_slot")
        self.correlation_ids.append(input.correlation_id)


async def _run(
    fakes: _RecordingActivities, workflow_id: str, correlation_id: str = "req-wf"
) -> None:
    async with await start_test_env() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AppointmentSchedulingWorkflow],
            activities=[
                fakes.validate_eligibility,
                fakes.reject,
                fakes.reserve_slot,
                fakes.billing_precheck,
                fakes.schedule_reminders,
                fakes.confirm,
                fakes.release_slot,
            ],
        ):
            await env.client.execute_workflow(
                AppointmentSchedulingWorkflow.run,
                AppointmentInput(1, correlation_id),
                id=workflow_id,
                task_queue=_TASK_QUEUE,
            )


async def test_scheduling_workflow_confirms_on_the_happy_path() -> None:
    fakes = _RecordingActivities()
    await _run(fakes, "test-scheduling-happy-path")

    assert fakes.calls == [
        "validate_eligibility",
        "reserve_slot",
        "billing_precheck",
        "schedule_reminders",
        "confirm",
    ]


async def test_scheduling_workflow_rejects_when_ineligible() -> None:
    fakes = _RecordingActivities(fail_eligibility=True)
    await _run(fakes, "test-scheduling-ineligible")

    assert fakes.calls == ["validate_eligibility", "reject"]


async def test_scheduling_workflow_rejects_when_slot_lost() -> None:
    fakes = _RecordingActivities(fail_reserve=True)
    await _run(fakes, "test-scheduling-slot-lost")

    assert fakes.calls == ["validate_eligibility", "reserve_slot", "reject"]


async def test_scheduling_workflow_compensates_on_billing_failure() -> None:
    fakes = _RecordingActivities(fail_billing=True)
    await _run(fakes, "test-scheduling-billing-failure")

    assert fakes.calls == [
        "validate_eligibility",
        "reserve_slot",
        "billing_precheck",
        "release_slot",
    ]


async def test_scheduling_workflow_reraises_unexpected_eligibility_error() -> None:
    # An error type the workflow doesn't recognize as APPOINTMENT_INELIGIBLE
    # must propagate, not be silently treated as a clean rejection --
    # that's the difference between "the booking was invalid" and "Temporal
    # needs to retry or a human needs to look at this."
    fakes = _RecordingActivities(
        fail_eligibility=True, eligibility_error_type="SOME_OTHER_ERROR"
    )

    with pytest.raises(WorkflowFailureError):
        await _run(fakes, "test-scheduling-unexpected-eligibility-error")

    assert fakes.calls == ["validate_eligibility"]


async def test_scheduling_workflow_reraises_unexpected_reserve_error() -> None:
    fakes = _RecordingActivities(
        fail_reserve=True, reserve_error_type="SOME_OTHER_ERROR"
    )

    with pytest.raises(WorkflowFailureError):
        await _run(fakes, "test-scheduling-unexpected-reserve-error")

    assert fakes.calls == ["validate_eligibility", "reserve_slot"]


async def test_scheduling_workflow_reraises_unexpected_billing_error() -> None:
    fakes = _RecordingActivities(
        fail_billing=True, billing_error_type="SOME_OTHER_ERROR"
    )

    with pytest.raises(WorkflowFailureError):
        await _run(fakes, "test-scheduling-unexpected-billing-error")

    assert fakes.calls == ["validate_eligibility", "reserve_slot", "billing_precheck"]


async def test_the_correlation_id_reaches_every_activity() -> None:
    """The whole point of threading the id through the workflow input: a
    booking's log lines from the API, the saga's Activities and the Celery
    reminder all carry one id.

    Asserting one id per recorded call, rather than just "it appears
    somewhere", is what would catch an Activity being invoked with a bare
    id instead of its input object -- that Activity would log under a
    freshly minted id and quietly fall out of the trace.

    The happy path's five Activities are named explicitly first, because
    comparing two lists built from the same run is vacuously true if both
    turn out empty.
    """
    fakes = _RecordingActivities()

    await _run(fakes, "test-scheduling-correlation")

    assert fakes.calls == [
        "validate_eligibility",
        "reserve_slot",
        "billing_precheck",
        "schedule_reminders",
        "confirm",
    ]
    assert fakes.correlation_ids == ["req-wf"] * 5
