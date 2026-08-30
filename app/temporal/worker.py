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
from app.temporal.activities import PublishActivities
from app.temporal.client import get_temporal_client
from app.temporal.workflows import PublishServiceWorkflow

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """Connect to Temporal and poll settings.temporal_task_queue until killed."""
    client = await get_temporal_client()
    activities = PublishActivities()
    # PublishActivities' methods are all plain def, not async def -- they
    # do blocking database I/O, so they need a thread pool to run in
    # rather than Temporal's own event loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[PublishServiceWorkflow],
            activities=[
                activities.validate_service,
                activities.structure_content,
                activities.chunk_content,
                activities.mark_published,
                activities.mark_publish_failed,
            ],
            activity_executor=executor,
        )
        logger.info(
            "worker started, polling task queue %s", settings.temporal_task_queue
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(run_worker())
