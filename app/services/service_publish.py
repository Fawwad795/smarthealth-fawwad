"""Legal entry actions on a Service's publish lifecycle.

Guards the two actions that can start next: publish (which the Week 2
Temporal workflow performs) and unpublish. Checked in one place, before
either action is allowed to start, so a client cannot start a second
publish workflow on a service that already has one running, or unpublish
something that was never published.

Matches the lifecycle exactly:

    DRAFT ──publish──> PUBLISHING ──success──> PUBLISHED ──unpublish──> INACTIVE
                            └──failure──> PUBLISH_FAILED ──retry──> PUBLISHING
    PUBLISHED or INACTIVE ──publish again──> PUBLISHING

PUBLISHING is never a legal source for either action -- a workflow is
already running against the row, and starting a second one would race it.

Publishing again goes straight from PUBLISHED to PUBLISHING (task 4.7),
not back through DRAFT: DRAFT would add a step that checks nothing. It is
how an edited service's search index catches up with its text.
"""

import logging

from fastapi import status
from sqlalchemy.orm import Session
from temporalio.service import RPCError

from app.ai.vector_store import get_vector_store
from app.core.config import settings
from app.core.logging import ensure_correlation_id
from app.core.exceptions import AppError
from app.models import Service
from app.models.enums import ServiceStatus
from app.schemas.service import PublishProgress
from app.services.service import get_service
from app.temporal.activities import ServiceInput
from app.temporal.client import get_temporal_client
from app.temporal.workflows import PublishServiceWorkflow

logger = logging.getLogger(__name__)

_PUBLISHABLE_FROM = {
    ServiceStatus.DRAFT,
    ServiceStatus.PUBLISH_FAILED,
    ServiceStatus.PUBLISHED,
    ServiceStatus.INACTIVE,
}
_UNPUBLISHABLE_FROM = {ServiceStatus.PUBLISHED}


def ensure_can_publish(service: Service) -> None:
    """Raise 409 unless `service.status` allows starting the publish workflow."""
    if service.status not in _PUBLISHABLE_FROM:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="SERVICE_NOT_PUBLISHABLE",
            message=(
                f"Cannot publish a service with status {service.status}. "
                "Its publish workflow is still running."
            ),
        )


def ensure_can_unpublish(service: Service) -> None:
    """Raise 409 unless `service.status` allows unpublishing."""
    if service.status not in _UNPUBLISHABLE_FROM:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="SERVICE_NOT_UNPUBLISHABLE",
            message=(
                f"Cannot unpublish a service with status {service.status}. "
                "Only PUBLISHED services can be unpublished."
            ),
        )


def publish_workflow_id(service_id: int) -> str:
    """The deterministic Temporal workflow id for this service's publish
    workflow. Reusing the same id on a second attempt is what lets
    Temporal itself reject a concurrent double-publish -- a second,
    independent guard alongside ensure_can_publish's status check.
    """
    return f"publish-service-{service_id}"


async def start_publish(db: Session, service_id: int) -> tuple[Service, str]:
    """Look up the service, confirm it's legal to publish, transition it
    to PUBLISHING, take its vectors out of search, and start its publish
    Workflow.

    Returns the service (now PUBLISHING) and the workflow id the caller
    hands back to the client. If starting the workflow fails, the status
    and the vectors' published flag are rolled back rather than left as
    if a publish were running -- a narrow window remains between the
    commit below and the Temporal call succeeding. This week's atomicity
    rigor is spent on slot reservation and the booking saga; this simpler
    linear workflow accepts that gap as a documented limitation (see
    docs/design.md).
    """
    service = _lock(db, service_id)
    ensure_can_publish(service)

    previous_status, previous_error = service.status, service.publish_error
    service.status = ServiceStatus.PUBLISHING
    # The last attempt's error no longer describes this one.
    service.publish_error = None
    # Re-publishing a PUBLISHED service: search filters on the vector's own
    # flag, not on services.status, so without this the old vectors would
    # stay searchable while the workflow replaces them. mark_published
    # flips them back in the same transaction as the PUBLISHED status. A
    # no-op for a DRAFT service, which has no vectors yet.
    get_vector_store(db).set_published(service.id, False)
    db.commit()
    db.refresh(service)

    workflow_id = publish_workflow_id(service.id)
    try:
        client = await get_temporal_client()
        await client.start_workflow(
            PublishServiceWorkflow.run,
            ServiceInput(service.id, ensure_correlation_id()),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
    except Exception:
        service.status = previous_status
        service.publish_error = previous_error
        get_vector_store(db).set_published(
            service.id, previous_status == ServiceStatus.PUBLISHED
        )
        db.commit()
        raise

    return service, workflow_id


def unpublish_service(db: Session, service_id: int) -> Service:
    """Take a PUBLISHED service out of the catalogue: status INACTIVE and
    its vectors out of search, in one transaction.

    Not a workflow: one row and one flag change together, with nothing to
    retry or resume, so a transaction is the whole job -- the same
    reasoning that keeps the visit lifecycle out of Temporal. The chunks
    and vectors are kept, so publishing the service again re-embeds only
    what has changed (task 4.9). published_at is kept too: it records
    when the service was last published, and status says it no longer is.
    """
    service = _lock(db, service_id)
    ensure_can_unpublish(service)

    service.status = ServiceStatus.INACTIVE
    get_vector_store(db).set_published(service.id, False)
    db.commit()
    db.refresh(service)
    logger.info("unpublished service %s", service.id)
    return service


def _lock(db: Session, service_id: int) -> Service:
    """Load the service and lock its row until the caller commits.

    The status check and the transition that follows must not interleave
    with another request's. Without the lock, two clicks on publish for a
    PUBLISHED service both read PUBLISHED and both pass; the one Temporal
    then refuses (same workflow id) rolls the row back to PUBLISHED while
    the other's workflow is still running. With it, the second request
    waits, then reads PUBLISHING and gets a 409. get_service first, so a
    missing id is still the usual 404.
    """
    service = get_service(db, service_id)
    db.refresh(service, with_for_update=True)
    return service


async def get_publish_progress(service: Service) -> PublishProgress | None:
    """What a running publish is doing now: the Activity it is on, which
    attempt, and why the previous attempt failed. None unless the service
    is PUBLISHING with an Activity scheduled or running.

    Read from Temporal, which tracks each pending Activity's attempt count
    and last failure; the service row only says PUBLISHING. A status read
    must still answer when Temporal is down, so a failure to ask is logged
    and reported as no progress rather than raised.
    """
    if service.status != ServiceStatus.PUBLISHING:
        return None
    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(publish_workflow_id(service.id))
        description = await handle.describe()
    except (RPCError, RuntimeError) as err:
        # RuntimeError is what Client.connect raises when the server is
        # unreachable; RPCError covers a workflow Temporal no longer has.
        logger.warning(
            "could not read publish progress service_id=%s: %s",
            service.id,
            type(err).__name__,
        )
        return None

    pending = description.raw_description.pending_activities
    if not pending:
        return None
    # The publish workflow runs one Activity at a time, so there is at most
    # one pending.
    current = pending[0]
    return PublishProgress(
        step=current.activity_type.name,
        attempt=current.attempt,
        # An unset protobuf message reads as empty, not None.
        last_error=current.last_failure.message or None,
    )
