"""Proves the Week 2 Temporal test-infra decision: WorkflowEnvironment's
time-skipping test server runs a real workflow end-to-end with no Docker
Temporal service and no worker container -- the same pattern
PublishServiceWorkflow's own tests will use.

Throwaway: deleted once PublishServiceWorkflow exists, whose own tests
prove the same plumbing against real application code instead of the
temporary PingWorkflow.
"""

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.core.config import settings
from app.temporal.ping_workflow import PingWorkflow, ping_activity


async def test_ping_workflow_runs_against_the_time_skipping_test_server():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=settings.temporal_task_queue,
            workflows=[PingWorkflow],
            activities=[ping_activity],
        ):
            result = await env.client.execute_workflow(
                PingWorkflow.run,
                "Fawwad",
                id="test-ping-workflow",
                task_queue=settings.temporal_task_queue,
            )

    assert result == "pong, Fawwad"
