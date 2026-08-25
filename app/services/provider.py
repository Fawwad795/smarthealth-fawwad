"""Business rules for provider profile management: create, list, get, update.

Creating a provider profile never creates the underlying account -- that
comes from the seed script (task 1.10) or, in Week 1, a row inserted by
hand. This module's job is only to attach the operational profile
(department, specialty, bio) to a user_id that already exists and is
already role PROVIDER.
"""

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Provider, User
from app.models.enums import UserRole
from app.schemas.provider import ProviderCreate, ProviderUpdate


def create_provider(db: Session, data: ProviderCreate) -> Provider:
    """Insert a provider profile, or fail if user_id doesn't exist, isn't
    role PROVIDER, or already has a profile.

    Checked explicitly and in this order rather than left to the database:
    Provider.user_id is unique, so a duplicate profile would raise an
    IntegrityError -- but that error can't distinguish "already a provider"
    from "department_id doesn't exist", and those need different responses.

    The role check is not cosmetic: a provider profile attached to a
    patient's account would hand that account provider-level access to
    schedules and slots.
    """
    user = db.get(User, data.user_id)
    if user is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="USER_NOT_FOUND",
            message="No user exists with this user_id.",
        )
    if user.role != UserRole.PROVIDER:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="USER_NOT_A_PROVIDER",
            message="This user's role is not PROVIDER.",
        )

    existing = db.execute(
        select(Provider).where(Provider.user_id == data.user_id)
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="PROVIDER_PROFILE_EXISTS",
            message="This user already has a provider profile.",
        )

    provider = Provider(
        user_id=data.user_id,
        department_id=data.department_id,
        specialty_id=data.specialty_id,
        bio=data.bio,
    )
    db.add(provider)
    try:
        db.commit()
    except IntegrityError:
        # Only department_id/specialty_id can still be wrong at this point
        # -- user_id existing and being unique were both checked above.
        db.rollback()
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="DEPARTMENT_OR_SPECIALTY_NOT_FOUND",
            message="No department or specialty exists with the given id.",
        )
    db.refresh(provider)
    return provider


def get_provider(db: Session, provider_id: int) -> Provider:
    """Fetch one provider profile by id, or raise 404.

    Note this is the Provider row's own id, not the user_id of the account
    it belongs to -- the two are different numbers.
    """
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="PROVIDER_NOT_FOUND",
            message="No provider exists with this id.",
        )
    return provider


def list_providers(
    db: Session, pagination: PaginationParams
) -> tuple[list[Provider], int]:
    """Return one page of provider profiles plus the unpaginated total.

    Grouped by department, which is how staff read this list; id breaks
    ties so the ordering is fully deterministic across pages.
    """
    total = db.execute(select(func.count()).select_from(Provider)).scalar_one()
    items = (
        db.execute(
            select(Provider)
            .order_by(Provider.department_id, Provider.id)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        .scalars()
        .all()
    )
    return list(items), total


def update_provider(
    db: Session, provider_id: int, data: ProviderUpdate
) -> Provider:
    """Move a provider between departments/specialties, or edit their bio.

    user_id is absent from ProviderUpdate and cannot be changed here:
    re-pointing a profile at a different account is not an edit, it means
    the wrong account was made a provider, which is a delete-and-recreate.

    The two foreign keys are validated by the database rather than
    pre-checked, since unlike create there is no second failure mode here
    for an IntegrityError to be confused with.
    """
    provider = get_provider(db, provider_id)

    if data.department_id is not None:
        provider.department_id = data.department_id
    if data.specialty_id is not None:
        provider.specialty_id = data.specialty_id
    if data.bio is not None:
        provider.bio = data.bio

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="DEPARTMENT_OR_SPECIALTY_NOT_FOUND",
            message="No department or specialty exists with the given id.",
        )
    db.refresh(provider)
    return provider
