"""Proves run_worker() wires the real Client, Workflows and Activities
together correctly -- without connecting to a real Temporal server or
polling forever. Both are replaced with fakes, monkeypatched where
worker.py uses them (not where they're defined) -- the same pattern
test_service_publish_routes.py already uses for get_temporal_client.
"""

import pytest

from app.core.config import settings
from app.temporal import worker as worker_module
from app.temporal.workflows import AppointmentSchedulingWorkflow, PublishServiceWorkflow


class _FakeWorker:
    """Records its own construction instead of connecting to a real
    Temporal server, and returns immediately from run() instead of
    polling forever. Keeps every instance built so the test can inspect
    what run_worker() actually wired up.
    """

    instances: list["_FakeWorker"] = []

    def __init__(self, client, *, task_queue, workflows, activities, activity_executor):
        self.client = client
        self.task_queue = task_queue
        self.workflows = workflows
        self.activities = activities
        _FakeWorker.instances.append(self)

    async def run(self) -> None:
        return None


async def _fake_get_temporal_client() -> object:
    # run_worker() only ever passes this straight through to Worker() --
    # a plain object is enough to stand in for it.
    return object()


async def test_run_worker_registers_both_workflows_and_every_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeWorker.instances.clear()
    monkeypatch.setattr(worker_module, "get_temporal_client", _fake_get_temporal_client)
    monkeypatch.setattr(worker_module, "Worker", _FakeWorker)

    await worker_module.run_worker()

    built = _FakeWorker.instances[0]
    assert built.task_queue == settings.temporal_task_queue
    assert set(built.workflows) == {
        PublishServiceWorkflow,
        AppointmentSchedulingWorkflow,
    }
    assert {a.__name__ for a in built.activities} == {
        "validate_service",
        "structure_content",
        "chunk_content",
        "mark_published",
        "mark_publish_failed",
        "validate_eligibility",
        "reject",
        "reserve_slot",
        "billing_precheck",
        "schedule_reminders",
        "confirm",
        "release_slot",
    }
