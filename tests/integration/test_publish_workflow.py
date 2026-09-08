"""Tests for PublishServiceWorkflow's orchestration logic: does it call the
right Activities in the right order, and does it branch correctly on a
validation failure? Runs against WorkflowEnvironment's time-skipping test
server with fake Activities that record call order instead of touching a
database -- the real Activities' database behaviour is already proven by
test_publish_activities.py. This is what test_temporal_infra.py's
docstring promised would replace it.
"""

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from app.temporal.activities import ChunkContentInput, ServiceInput
from app.temporal.workflows import PublishServiceWorkflow
from tests.temporal_env import start_test_env

_TASK_QUEUE = "test_publish-workflow"


class _RecordingActivities:
    """Fakes for the four Activities: same names (so Temporal resolves
    them the same way it would resolve PublishActivities' real methods),
    but they record call order instead of touching a database.
    """

    def __init__(self, fail_validation: bool = False) -> None:
        self.calls: list[str] = []
        self._fail_validation = fail_validation

    @activity.defn
    async def validate_service(self, input: ServiceInput) -> None:
        self.calls.append("validate_service")
        if self._fail_validation:
            raise ApplicationError(
                "description is required",
                type="SERVICE_INCOMPLETE",
                non_retryable=True,
            )

    @activity.defn
    async def structure_content(self, input: ServiceInput) -> str:
        self.calls.append("structure_content")
        return "structured text"

    @activity.defn
    async def chunk_content(self, input: ChunkContentInput) -> int:
        self.calls.append("chunk_content")
        return 1

    @activity.defn
    async def mark_published(self, input: ServiceInput) -> None:
        self.calls.append("mark_published")

    @activity.defn
    async def mark_publish_failed(self, input: ServiceInput) -> None:
        self.calls.append("mark_publish_failed")


async def test_publish_workflow_runs_every_step_in_order() -> None:
    fakes = _RecordingActivities()
    async with await start_test_env() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[PublishServiceWorkflow],
            activities=[
                fakes.validate_service,
                fakes.structure_content,
                fakes.chunk_content,
                fakes.mark_published,
                fakes.mark_publish_failed,
            ],
        ):
            await env.client.execute_workflow(
                PublishServiceWorkflow.run,
                ServiceInput(1),
                id="test-publish-happy-path",
                task_queue=_TASK_QUEUE,
            )

    assert fakes.calls == [
        "validate_service",
        "structure_content",
        "chunk_content",
        "mark_published",
    ]


async def test_publish_workflow_stops_cleanly_on_validation_failure() -> None:
    fakes = _RecordingActivities(fail_validation=True)
    async with await start_test_env() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[PublishServiceWorkflow],
            activities=[
                fakes.validate_service,
                fakes.structure_content,
                fakes.chunk_content,
                fakes.mark_published,
                fakes.mark_publish_failed,
            ],
        ):
            await env.client.execute_workflow(
                PublishServiceWorkflow.run,
                ServiceInput(1),
                id="test-publish-validation-failure",
                task_queue=_TASK_QUEUE,
            )

    assert fakes.calls == ["validate_service", "mark_publish_failed"]
