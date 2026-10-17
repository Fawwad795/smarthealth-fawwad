"""Fixtures shared by the integration tests that run the real publish
workflow: the seed script's tests and the re-index tests (task 4.7).
"""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
from sqlalchemy.orm import Session
from temporalio.client import Client
from temporalio.worker import Worker

from app.ai.embeddings import FakeEmbeddings
from app.core.config import settings
from app.temporal.activities import PublishActivities
from app.temporal.workflows import PublishServiceWorkflow
from tests.temporal_env import start_test_env, use_test_client


@pytest.fixture()
async def publishing_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Client]:
    """A Temporal test server with a worker running the real publish
    Activities against the test database, embedding with FakeEmbeddings so
    nothing reaches the network.

    The worker listens on the real task queue because start_publish()
    reads it from settings. The Activities are plain functions, so the
    worker needs a thread pool to run them; one thread keeps them in
    sequence, which db_session (not thread-safe) relies on.
    """
    activities = PublishActivities(
        session_factory=lambda: nullcontext(db_session),
        embeddings_factory=FakeEmbeddings,
    )
    async with await start_test_env() as env:
        use_test_client(monkeypatch, env.client)
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(
                env.client,
                task_queue=settings.temporal_task_queue,
                workflows=[PublishServiceWorkflow],
                activities=[
                    activities.validate_service,
                    activities.structure_content,
                    activities.chunk_content,
                    activities.embed_chunks,
                    activities.mark_published,
                    activities.mark_publish_failed,
                ],
                activity_executor=executor,
            ):
                yield env.client
