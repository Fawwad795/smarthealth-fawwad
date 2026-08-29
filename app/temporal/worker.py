"""Entrypoint for the Temporal worker process.

Run as its own container (the `temporal-worker` service in docker-compose.yml,
same image as the API, different command) so it can poll for work
independently of whether the API process is up. Registers every workflow
and activity the app defines; 2.3 adds the real publish workflow here.
"""

import asyncio
import logging

from temporalio.worker import Worker

from app.core.config import settings
from app.temporal.client import get_temporal_client
from app.temporal.ping_workflow import PingWorkflow, ping_activity

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """Connect to Temporal and poll settings.temporal_task_queue until killed."""
    client = await get_temporal_client()
    worker = Worker(
        client, 
        task_queue=settings.temporal_task_queue,
        workflows=[PingWorkflow],
        activities=[ping_activity],
    )
    logger.info("worker started, polling task queue %s", settings.temporal_task_queue)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(run_worker())