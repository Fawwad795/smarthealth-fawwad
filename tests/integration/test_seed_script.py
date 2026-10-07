"""The seed script: builds a demo dataset, and is safe to run twice.

Not exercised via subprocess -- scripts/seed.py's seed() function is
imported directly and run against the test database, the same way every
other integration test in this suite calls into a service module.

publish_services() is tested against Temporal's test server, with a
worker running the real publish Activities on this test's db_session, so
the chunk it writes can be read back here.
"""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from temporalio.client import Client
from temporalio.worker import Worker

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models import (
    Clinic,
    ContentChunk,
    Department,
    Patient,
    Provider,
    ProviderService,
    Service,
    Slot,
    User,
)
from app.models.enums import ServiceStatus
from app.temporal.activities import PublishActivities
from app.temporal.workflows import PublishServiceWorkflow
from scripts.seed import SEED_ACCOUNTS, SEED_PASSWORD, publish_services, seed
from tests.temporal_env import start_test_env


def _count(db: Session, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def test_seed_creates_the_expected_entities(db_session: Session) -> None:
    seed(db_session)

    assert _count(db_session, Clinic) == 1
    assert _count(db_session, Department) == 3
    assert _count(db_session, Provider) == 3
    assert _count(db_session, Service) == 3
    assert _count(db_session, Patient) == 3
    assert _count(db_session, Slot) > 0


def test_seed_is_idempotent(db_session: Session) -> None:
    seed(db_session)
    models = (Clinic, Department, Provider, Service, Patient, User, Slot)
    first_counts = {model: _count(db_session, model) for model in models}

    seed(db_session)
    second_counts = {model: _count(db_session, model) for model in models}

    assert first_counts == second_counts


def test_seed_repairs_a_drifted_password(db_session: Session) -> None:
    """A seeded account whose password changed is restored by a re-run.

    Pins the real defect: get-or-create returned early on an existing
    row, so a broken seed login stayed broken however often you re-ran.
    """
    seed(db_session)
    user = db_session.execute(
        select(User).where(User.email == "patient.one@example.com")
    ).scalar_one()
    user.password_hash = hash_password("not-the-seed-password")
    db_session.flush()

    seed(db_session)

    assert verify_password(SEED_PASSWORD, user.password_hash)


def test_advertised_accounts_match_what_seed_creates(db_session: Session) -> None:
    """SEED_ACCOUNTS must be exactly the accounts seed() inserts.

    Without this the printed list is decoration: adding a patient and
    forgetting the list would advertise nothing about a working login,
    and removing one would advertise a login that does not exist.
    """
    seed(db_session)

    emails = set(db_session.execute(select(User.email)).scalars().all())

    assert set(SEED_ACCOUNTS) == emails


def test_seed_repairs_an_unparseable_password_hash(db_session: Session) -> None:
    """A hash passlib cannot parse is repaired, not re-raised.

    Found by running the seed against a corrupted row: the first version
    called verify_password() unguarded, so the script crashed on the one
    account it was there to fix. Logging in with such a row still 500s --
    corruption is a bug for the catch-all, not an AppError -- which makes
    re-running the seed the only recovery, so it must not crash.
    """
    seed(db_session)
    user = db_session.execute(
        select(User).where(User.email == "patient.one@example.com")
    ).scalar_one()
    user.password_hash = "$2b$12$not-a-real-bcrypt-hash"
    db_session.flush()

    seed(db_session)

    assert verify_password(SEED_PASSWORD, user.password_hash)


def _use_test_client(monkeypatch: pytest.MonkeyPatch, client: Client) -> None:
    """Make start_publish() start its workflow on the test server.

    Patched where service_publish.py uses get_temporal_client, not where
    it is defined, the same as the publish route tests.
    """

    async def _get_test_client() -> Client:
        return client

    monkeypatch.setattr(
        "app.services.service_publish.get_temporal_client", _get_test_client
    )


@pytest.fixture()
async def publishing_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Client]:
    """A Temporal test server with a worker running the real publish
    Activities against the test database.

    The worker listens on the real task queue because start_publish()
    reads it from settings. The Activities are plain functions, so the
    worker needs a thread pool to run them; one thread keeps them in
    sequence, which db_session (not thread-safe) relies on.
    """
    activities = PublishActivities(session_factory=lambda: nullcontext(db_session))
    async with await start_test_env() as env:
        _use_test_client(monkeypatch, env.client)
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(
                env.client,
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
            ):
                yield env.client


def _chunks_for(db: Session, service: Service) -> list[ContentChunk]:
    return list(
        db.scalars(select(ContentChunk).where(ContentChunk.source_id == service.id))
    )


def test_seed_leaves_services_draft_and_linked_to_a_provider(
    db_session: Session,
) -> None:
    """seed() itself publishes nothing; the workflow is the only path to
    PUBLISHED.

    Pins the Week 1 shortcut that set PUBLISHED directly: those services
    had no chunk, so search would have had nothing to find.
    """
    services = seed(db_session)

    assert len(services) == 3
    for service in services:
        assert service.status == ServiceStatus.DRAFT
        assert _chunks_for(db_session, service) == []
        links = db_session.scalars(
            select(ProviderService).where(ProviderService.service_id == service.id)
        ).all()
        assert len(links) == 1


async def test_publish_services_publishes_each_service_with_its_chunk(
    db_session: Session, publishing_client: Client
) -> None:
    services = seed(db_session)

    await publish_services(db_session, services, publishing_client)

    for service in services:
        assert service.status == ServiceStatus.PUBLISHED
        assert service.published_at is not None
        assert len(_chunks_for(db_session, service)) == 1

    # The provider links existed before publishing, so the chunk carries the
    # linked provider's specialty. Knee X-Ray is the seeded service whose
    # specialty ("Orthopedics") is not dropped as a repeat of its department
    # ("Orthopaedics").
    knee_xray = next(s for s in services if s.name == "Knee X-Ray")
    [knee_chunk] = _chunks_for(db_session, knee_xray)
    assert knee_chunk.text.startswith("Orthopaedics · Orthopedics: Knee X-Ray.")


async def test_publish_services_leaves_published_services_alone_on_a_rerun(
    db_session: Session, publishing_client: Client
) -> None:
    """A second seed run must not publish again.

    chunk_content deletes and re-inserts a service's chunk, so an
    unchanged chunk id proves no second workflow ran.
    """
    services = seed(db_session)
    await publish_services(db_session, services, publishing_client)
    first_chunk_ids = {s.id: _chunks_for(db_session, s)[0].id for s in services}

    await publish_services(db_session, seed(db_session), publishing_client)

    for service in services:
        assert service.status == ServiceStatus.PUBLISHED
        [chunk] = _chunks_for(db_session, service)
        assert chunk.id == first_chunk_ids[service.id]


async def test_publish_services_stops_with_a_message_when_no_worker_runs(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no worker the workflow is accepted but never runs, so waiting
    on it would hang the seed forever. It must stop and say why.
    """
    services = seed(db_session)
    monkeypatch.setattr("scripts.seed.PUBLISH_TIMEOUT_SECONDS", 0.5)
    async with await start_test_env() as env:
        _use_test_client(monkeypatch, env.client)

        with pytest.raises(SystemExit, match="Is temporal-worker running"):
            await publish_services(db_session, services, env.client)

    assert services[0].status == ServiceStatus.PUBLISHING
