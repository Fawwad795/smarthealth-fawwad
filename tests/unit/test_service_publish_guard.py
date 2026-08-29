"""The service publish/unpublish transition guard, tested with no database --
these functions only ever look at an already-loaded Service object.
"""

import pytest

from app.core.exceptions import AppError
from app.models import Service
from app.models.enums import ServiceStatus
from app.services.service_publish import ensure_can_publish, ensure_can_unpublish


def _service(status: ServiceStatus) -> Service:
    return Service(id=1, department_id=1, name="X-Ray", status=status)


@pytest.mark.parametrize("status", [ServiceStatus.DRAFT, ServiceStatus.PUBLISH_FAILED])
def test_publish_allowed_from_draft_or_publish_failed(status: ServiceStatus) -> None:
    ensure_can_publish(_service(status))  # no exception


@pytest.mark.parametrize(
    "status",
    [ServiceStatus.PUBLISHING, ServiceStatus.PUBLISHED, ServiceStatus.INACTIVE],
)
def test_publish_rejected_from_every_other_status(status: ServiceStatus) -> None:
    with pytest.raises(AppError) as exc_info:
        ensure_can_publish(_service(status))
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "SERVICE_NOT_PUBLISHABLE"


def test_unpublish_allowed_from_published() -> None:
    ensure_can_unpublish(_service(ServiceStatus.PUBLISHED))  # no exception


@pytest.mark.parametrize(
    "status",
    [
        ServiceStatus.DRAFT,
        ServiceStatus.PUBLISHING,
        ServiceStatus.PUBLISH_FAILED,
        ServiceStatus.INACTIVE,
    ],
)
def test_unpublish_rejected_from_every_other_status(status: ServiceStatus) -> None:
    with pytest.raises(AppError) as exc_info:
        ensure_can_unpublish(_service(status))
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "SERVICE_NOT_UNPUBLISHABLE"