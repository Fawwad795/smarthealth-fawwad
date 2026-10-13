"""Proves start_publish's row lock: many concurrent publishes of one
PUBLISHED service, exactly one starts a workflow.

Built like test_slot_service.py, and for the same reason it does not use
db_session: the racing requests need their own connections, which can only
see a row that is really committed. Unlike that test, this one deletes what
it committed, because other tests count services.

Temporal is replaced by a client that refuses a second start of the same
workflow id, as the real server does.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.core.exceptions import AppError
from app.models import Clinic, Department, Service
from app.models.enums import ServiceStatus
from app.services import service_publish

ATTEMPTS = 20


class _OneStartPerIdClient:
    """Starts a workflow id once, then raises what Temporal raises."""

    def __init__(self) -> None:
        self._started: set[str] = set()
        self._lock = threading.Lock()

    async def start_workflow(self, *args: Any, id: str, **kwargs: Any) -> None:
        with self._lock:
            if id in self._started:
                raise WorkflowAlreadyStartedError(id, "PublishServiceWorkflow")
            self._started.add(id)


def _attempt_publish(engine, service_id: int) -> str:
    """One request's attempt, on its own session and connection: "started",
    or the error code it got."""
    session = Session(bind=engine)
    try:
        asyncio.run(service_publish.start_publish(session, service_id))
        return "started"
    except AppError as err:
        return err.code
    except WorkflowAlreadyStartedError:
        return "refused by Temporal"
    finally:
        session.close()


def test_concurrent_republishes_exactly_one_starts(
    test_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _OneStartPerIdClient()

    async def _get_fake() -> _OneStartPerIdClient:
        return fake

    monkeypatch.setattr("app.services.service_publish.get_temporal_client", _get_fake)
    engine = create_engine(test_database, pool_size=ATTEMPTS, max_overflow=0)
    setup = Session(bind=engine)
    clinic = Clinic(name="Publish Concurrency Clinic", timezone="UTC")
    setup.add(clinic)
    setup.flush()
    department = Department(
        clinic_id=clinic.id, name="Publish Concurrency Dept", order_index=1
    )
    setup.add(department)
    setup.flush()
    service = Service(
        department_id=department.id,
        name="Publish Concurrency Service",
        status=ServiceStatus.PUBLISHED,
    )
    setup.add(service)
    setup.commit()
    ids = (clinic.id, department.id, service.id)

    try:
        with ThreadPoolExecutor(max_workers=ATTEMPTS) as pool:
            results = list(
                pool.map(lambda _: _attempt_publish(engine, ids[2]), range(ATTEMPTS))
            )

        assert results.count("started") == 1
        assert results.count("SERVICE_NOT_PUBLISHABLE") == ATTEMPTS - 1
        setup.expire_all()
        assert setup.get(Service, ids[2]).status == ServiceStatus.PUBLISHING
    finally:
        setup.rollback()
        setup.execute(delete(Service).where(Service.id == ids[2]))
        setup.execute(delete(Department).where(Department.id == ids[1]))
        setup.execute(delete(Clinic).where(Clinic.id == ids[0]))
        setup.commit()
        setup.close()
        engine.dispose()
