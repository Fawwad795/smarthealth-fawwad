"""Activities for the service-publishing Workflow.

All I/O for the publish pipeline lives here, never in workflows.py --
Temporal Workflows must be deterministic and cannot touch the database
directly. Bundled on a class rather than left as bare functions so a test
can hand the constructor a factory pointing at the test database instead
of the real one; Activities have no Depends(get_db) to intercept the way
routes do.
"""

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.db.session import SessionLocal
from app.models import ContentChunk, Service
from app.models.enums import ContentSourceType, ServiceStatus


@dataclass
class ChunkContentInput:
    """Bundles chunk_content's two arguments into one type.

    Temporal recommends a single object over multiple positional
    arguments: a workflow already running in production can add a field
    to a dataclass without breaking replay of history recorded before the
    field existed, the way inserting a new positional argument would.
    """

    service_id: int
    text: str


class PublishActivities:
    """The four Activities the publish Workflow calls, in order.

    session_factory defaults to the real SessionLocal for production use.
    Tests construct this with a factory that hands back the test's own
    db_session instead, so every method below runs against the test
    database without any of the Activity code knowing the difference.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] = SessionLocal,
    ) -> None:
        """Store the session factory; open nothing until a method runs."""
        self._session_factory = session_factory

    @activity.defn
    def validate_service(self, service_id: int) -> None:
        """Raise a non-retryable error listing every missing field, or
        return None if the service has everything the workflow needs.

        Non-retryable: a missing description is still missing on the next
        attempt, so retrying wastes time instead of fixing anything.
        Temporal's default retry policy would otherwise keep retrying this
        for hours.
        """
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            errors = []
            if not service.description:
                errors.append("description is required")
            if not service.prep_instructions:
                errors.append("prep_instructions is required")
            if errors:
                raise ApplicationError(
                    "; ".join(errors),
                    type="SERVICE_INCOMPLETE",
                    non_retryable=True,
                )

    @activity.defn
    def structure_content(self, service_id: int) -> str:
        """Combine this service's fields into one enriched text block --
        the input the chunk activity splits into content_chunks rows.

        Department name is included so a patient's query ("cardiology
        check-up") can match on more than the bare service name. Provider
        specialty is a documented Week 4 improvement (task 4.3), not added
        here -- a service isn't tied to one single provider.
        """
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            return (
                f"{service.department.name}: {service.name}. "
                f"{service.description} Preparation: {service.prep_instructions}"
            )

    @activity.defn
    def chunk_content(self, input: ChunkContentInput) -> int:
        """Replace this service's content_chunks with one chunk holding
        `input.text`, and return how many chunks were written.

        Delete-then-insert in one transaction, not an incremental diff:
        this is both how re-publishing "replaces chunks atomically" (the
        Definition of Done) and how a retried attempt stays idempotent --
        run once or run twice, the end state is identical either way.
        """
        with self._session_factory() as db:
            db.execute(
                delete(ContentChunk).where(
                    ContentChunk.source_type == ContentSourceType.SERVICE,
                    ContentChunk.source_id == input.service_id,
                )
            )
            db.add(
                ContentChunk(
                    source_type=ContentSourceType.SERVICE,
                    source_id=input.service_id,
                    chunk_index=0,
                    text=input.text,
                    # A real tokenizer arrives with Week 4's embedding step;
                    # this rough estimate is enough for now.
                    token_count=len(input.text) // 4,
                    text_hash=hashlib.sha256(input.text.encode()).hexdigest(),
                )
            )
            db.commit()
            return 1

    @activity.defn
    def mark_published(self, service_id: int) -> None:
        """Transition the service to PUBLISHED -- the workflow's last step."""
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            service.status = ServiceStatus.PUBLISHED
            service.published_at = datetime.now(UTC)
            db.commit()

    @activity.defn
    def mark_publish_failed(self, service_id: int) -> None:
        """Transition the service to PUBLISH_FAILED.

        Called only after validate_service's non-retryable rejection --
        the Workflow's clean-failure path, not something Activities decide
        for themselves.
        """
        with self._session_factory() as db:
            service = db.get(Service, service_id)
            service.status = ServiceStatus.PUBLISH_FAILED
            db.commit()
