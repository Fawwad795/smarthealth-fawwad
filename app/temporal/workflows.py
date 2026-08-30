"""The service-publishing Workflow: orchestration only.

Calls validate_service -> structure_content -> chunk_content ->
mark_published in order. All I/O lives in the Activities this calls (see
activities.py) -- nothing here touches the database, the clock, or
anything else non-deterministic, because Temporal replays this function
on recovery and it must make the same decisions every time.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities import ChunkContentInput, PublishActivities

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
    async def run(self, service_id: int) -> None:
        """Run the publish pipeline for this service."""
        try:
            await workflow.execute_activity_method(
                PublishActivities.validate_service,
                service_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        except ActivityError as err:
            if (
                isinstance(err.cause, ApplicationError)
                and err.cause.type == "SERVICE_INCOMPLETE"
            ):
                await workflow.execute_activity_method(
                    PublishActivities.mark_publish_failed,
                    service_id,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )
                return
            raise

        structured_text = await workflow.execute_activity_method(
            PublishActivities.structure_content,
            service_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )

        await workflow.execute_activity_method(
            PublishActivities.chunk_content,
            ChunkContentInput(service_id, structured_text),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )

        await workflow.execute_activity_method(
            PublishActivities.mark_published,
            service_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
