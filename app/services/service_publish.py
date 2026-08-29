"""Legal entry actions on a Service's publish lifecycle.

Guards the two actions that can start next: publish (which the Week 2
Temporal workflow performs) and unpublish. Checked in one place, before
either action is allowed to start, so a client cannot start a second
publish workflow on a service that already has one running, or unpublish
something that was never published.

Matches the lifecycle exactly:

    DRAFT ──publish──> PUBLISHING ──success──> PUBLISHED ──unpublish──> INACTIVE
                            └──failure──> PUBLISH_FAILED ──retry──> PUBLISHING

PUBLISHING is never a legal source for either action -- a workflow is
already running against the row, and starting a second one would race it.
"""

from fastapi import status

from app.core.exceptions import AppError
from app.models import Service
from app.models.enums import ServiceStatus

_PUBLISHABLE_FROM = {ServiceStatus.DRAFT, ServiceStatus.PUBLISH_FAILED}
_UNPUBLISHABLE_FROM = {ServiceStatus.PUBLISHED}


def ensure_can_publish(service: Service) -> None:
    """Raise 409 unless `service.status` allows starting the publish workflow."""
    if service.status not in _PUBLISHABLE_FROM:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="SERVICE_NOT_PUBLISHABLE",
            message=(
                f"Cannot publish a service with status {service.status}. "
                "Only DRAFT or PUBLISH_FAILED services can be published."
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