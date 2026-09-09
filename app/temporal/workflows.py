"""The service-publishing and appointment-scheduling Workflows: orchestration only.

PublishServiceWorkflow calls validate_service -> structure_content ->
chunk_content -> mark_published in order. 

AppointmentSchedulingWorkflow calls validate_eligibility -> reserve_slot -> billing_precheck ->
schedule_reminders -> confirm, compensating (release_slot) if billing
fails after the slot was already reserved. 

All I/O lives in the Activities these call (see activities.py) -- nothing here touches the
database, the clock, or anything else non-deterministic, because
Temporal replays this function on recovery and it must make the same
decisions every time.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities import (
        AppointmentInput,
        ChunkContentInput,
        PublishActivities,
        RejectInput,
        ReleaseSlotInput,
        SchedulingActivities,
        ServiceInput,
    )

_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class PublishServiceWorkflow:
    """Publishes one service: validate -> structure -> chunk -> mark PUBLISHED.

    A validation failure ends the workflow cleanly in PUBLISH_FAILED --
    it is an expected, deliberate outcome, not a bug. Any other Activity
    failure (a dropped DB connection, say) is left to Temporal's default
    retry policy and, if that's exhausted, fails the workflow execution
    itself -- the correct signal for something that needs a human, not a
    resubmission.
    """

    @workflow.run
    async def run(self, input: ServiceInput) -> None:
        """Run the publish pipeline for this service."""
        try:
            await workflow.execute_activity_method(
                PublishActivities.validate_service,
                input,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        except ActivityError as err:
            if (
                isinstance(err.cause, ApplicationError)
                and err.cause.type == "SERVICE_INCOMPLETE"
            ):
                await workflow.execute_activity_method(
                    PublishActivities.mark_publish_failed,
                    input,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )
                return
            raise

        structured_text = await workflow.execute_activity_method(
            PublishActivities.structure_content,
            input,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )

        await workflow.execute_activity_method(
            PublishActivities.chunk_content,
            ChunkContentInput(input.service_id, structured_text, input.correlation_id),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )

        await workflow.execute_activity_method(
            PublishActivities.mark_published,
            input,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )


@workflow.defn
class AppointmentSchedulingWorkflow:
    """Schedules one appointment: the saga task 2.9 is built around.

    Three of its five steps can fail in a way that's expected, not a bug,
    and each has its own compensating action: an ineligible request or a
    lost slot race both end in REJECTED (nothing to undo -- the slot was
    never actually held by this appointment in the lost-race case, or
    never reserved at all in the ineligible case); a billing failure
    *after* the slot was reserved runs release_slot to give it back. Any
    other Activity failure is left to Temporal's default retry policy,
    same reasoning as PublishServiceWorkflow.

    The correlation id travels through as part of each Workflow's input and is
    passed on to every Activity. Nothing here reads or sets it -- a Workflow
    must stay deterministic, and re-establishing context is the Activity's job.
    """

    @workflow.run
    async def run(self, input: AppointmentInput) -> None:
        """Run the scheduling saga for this appointment."""
        try:
            await workflow.execute_activity_method(
                SchedulingActivities.validate_eligibility,
                input,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        except ActivityError as err:
            if (
                isinstance(err.cause, ApplicationError)
                and err.cause.type == "APPOINTMENT_INELIGIBLE"
            ):
                await workflow.execute_activity_method(
                    SchedulingActivities.reject,
                    RejectInput(
                        input.appointment_id, err.cause.message, input.correlation_id
                    ),
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )
                return
            raise

        try:
            await workflow.execute_activity_method(
                SchedulingActivities.reserve_slot,
                input,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        except ActivityError as err:
            if (
                isinstance(err.cause, ApplicationError)
                and err.cause.type == "SLOT_UNAVAILABLE"
            ):
                await workflow.execute_activity_method(
                    SchedulingActivities.reject,
                    RejectInput(
                        input.appointment_id, err.cause.message, input.correlation_id
                    ),
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )
                return
            raise

        try:
            await workflow.execute_activity_method(
                SchedulingActivities.billing_precheck,
                input,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        except ActivityError as err:
            if (
                isinstance(err.cause, ApplicationError)
                and err.cause.type == "BILLING_FAILED"
            ):
                await workflow.execute_activity_method(
                    SchedulingActivities.release_slot,
                    ReleaseSlotInput(
                        input.appointment_id, err.cause.message, input.correlation_id
                    ),
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )
                return
            raise

        await workflow.execute_activity_method(
            SchedulingActivities.schedule_reminders,
            input,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )

        await workflow.execute_activity_method(
            SchedulingActivities.confirm,
            input,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
