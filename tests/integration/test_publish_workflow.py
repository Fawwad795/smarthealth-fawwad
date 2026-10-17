"""Tests for PublishServiceWorkflow's orchestration logic: does it call the
right Activities in the right order, and does it branch correctly on a
validation or embedding failure? Runs against WorkflowEnvironment's
time-skipping test server with fake Activities that record call order
instead of touching a database -- the real Activities' database behaviour
is already proven by test_publish_activities.py. This is what
test_temporal_infra.py's docstring promised would replace it.

The time-skipping server also skips the embedding retry policy's waits, so
a test that makes five attempts takes no real time.
"""

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from app.temporal.activities import (
    ChunkContentInput,
    PublishFailedInput,
    ServiceInput,
)
from app.temporal.workflows import PublishServiceWorkflow
from tests.temporal_env import start_test_env

_TASK_QUEUE = "test_publish-workflow"

_PUBLISH_STEPS = ["validate_service", "structure_content", "chunk_content"]


class _RecordingActivities:
    """Fakes for the publish Activities: same names (so Temporal resolves
    them the same way it would resolve PublishActivities' real methods),
    but they record call order instead of touching a database.

    embed_failures holds what embed_chunks raises on its first attempts, one
    per attempt; once they run out, it succeeds.
    """

    def __init__(
        self,
        fail_validation: bool = False,
        embed_failures: list[Exception] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.failure_reasons: list[str | None] = []
        self._fail_validation = fail_validation
        self._embed_failures = list(embed_failures or [])

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
    async def embed_chunks(self, input: ServiceInput) -> int:
        self.calls.append("embed_chunks")
        if self._embed_failures:
            raise self._embed_failures.pop(0)
        return 1

    @activity.defn
    async def mark_published(self, input: ServiceInput) -> None:
        self.calls.append("mark_published")

    @activity.defn
    async def mark_publish_failed(self, input: PublishFailedInput) -> None:
        self.calls.append("mark_publish_failed")
        self.failure_reasons.append(input.reason)

    def all(self) -> list:
        """Every fake, for the Worker's activities list."""
        return [
            self.validate_service,
            self.structure_content,
            self.chunk_content,
            self.embed_chunks,
            self.mark_published,
            self.mark_publish_failed,
        ]


async def _run_publish(fakes: _RecordingActivities, workflow_id: str) -> None:
    """Run one publish workflow to the end against `fakes`."""
    async with await start_test_env() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[PublishServiceWorkflow],
            activities=fakes.all(),
        ):
            await env.client.execute_workflow(
                PublishServiceWorkflow.run,
                ServiceInput(1),
                id=workflow_id,
                task_queue=_TASK_QUEUE,
            )


async def test_publish_workflow_runs_every_step_in_order() -> None:
    fakes = _RecordingActivities()

    await _run_publish(fakes, "test-publish-happy-path")

    assert fakes.calls == [*_PUBLISH_STEPS, "embed_chunks", "mark_published"]


async def test_publish_workflow_stops_cleanly_on_validation_failure() -> None:
    fakes = _RecordingActivities(fail_validation=True)

    await _run_publish(fakes, "test-publish-validation-failure")

    assert fakes.calls == ["validate_service", "mark_publish_failed"]
    assert fakes.failure_reasons == ["description is required"]


async def test_publish_workflow_retries_a_transient_embedding_failure() -> None:
    """A model still loading (Hugging Face's 503) clears on a later try."""
    fakes = _RecordingActivities(
        embed_failures=[RuntimeError("HTTP 503") for _ in range(2)]
    )

    await _run_publish(fakes, "test-publish-embed-retried")

    assert fakes.calls == [*_PUBLISH_STEPS, *["embed_chunks"] * 3, "mark_published"]


async def test_publish_workflow_gives_up_on_embedding_after_five_attempts() -> None:
    """A provider that stays down fails the publish with its error, rather
    than leaving the service in PUBLISHING while Temporal retries forever.
    """
    fakes = _RecordingActivities(
        embed_failures=[RuntimeError("HTTP 503") for _ in range(5)]
    )

    await _run_publish(fakes, "test-publish-embed-exhausted")

    assert fakes.calls == [
        *_PUBLISH_STEPS,
        *["embed_chunks"] * 5,
        "mark_publish_failed",
    ]
    assert fakes.failure_reasons == ["HTTP 503"]


async def test_publish_workflow_does_not_retry_a_permanent_embedding_failure() -> None:
    """A rejected key is rejected again on every attempt; one is enough."""
    fakes = _RecordingActivities(
        embed_failures=[
            ApplicationError(
                "Hugging Face returned HTTP 401",
                type="EMBEDDING_FAILED",
                non_retryable=True,
            )
        ]
    )

    await _run_publish(fakes, "test-publish-embed-permanent")

    assert fakes.calls == [*_PUBLISH_STEPS, "embed_chunks", "mark_publish_failed"]
    assert fakes.failure_reasons == ["Hugging Face returned HTTP 401"]
