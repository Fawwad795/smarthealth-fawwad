"""Entrypoint for the Temporal worker process.

Run as its own container (the `temporal-worker` service in docker-compose.yml,
same image as the API, different command) so it can poll for work
independently of whether the API process is up. Registers every workflow
and activity the app defines.
"""

import asyncio
import concurrent.futures
import logging

from temporalio.worker import Worker

from app.core.config import settings
from app.core.logging import configure_logging
from app.temporal.activities import PublishActivities, SchedulingActivities
from app.temporal.client import get_temporal_client
from app.temporal.workflows import PublishServiceWorkflow, AppointmentSchedulingWorkflow

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """Connect to Temporal and poll settings.temporal_task_queue until killed."""
    client = await get_temporal_client()
    activities = PublishActivities()
    scheduling_activities = SchedulingActivities()
    # Both Activities classes are all plain def, not async def -- they do
    # blocking database I/O, so they need a thread pool to run in rather
    # than Temporal's own event loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[PublishServiceWorkflow, AppointmentSchedulingWorkflow],
            activities=[
                activities.validate_service,
                activities.structure_content,
                activities.chunk_content,
                activities.mark_published,
                activities.mark_publish_failed,
                scheduling_activities.validate_eligibility,
                scheduling_activities.reject,
                scheduling_activities.reserve_slot,
                scheduling_activities.billing_precheck,
                scheduling_activities.schedule_reminders,
                scheduling_activities.confirm,
                scheduling_activities.release_slot,
            ],
            activity_executor=executor,
        )
        logger.info(
            "worker started, polling task queue %s", settings.temporal_task_queue
        )
        await worker.run()


if __name__ == "__main__":
    # Configured here rather than at import: this block is the real entry
    # point, and the test that calls run_worker() directly should not have
    # the root logger swapped out from under it.
    configure_logging()
    asyncio.run(run_worker())
