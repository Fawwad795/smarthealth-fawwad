"""Business rules for provider profile management: create, list, get, update.

Creating a provider profile never creates the underlying account -- that
comes from the seed script (task 1.10) or, in Week 1, a row inserted by
hand. This module's job is only to attach the operational profile
(department, specialty, bio) to a user_id that already exists and is
already role PROVIDER.
"""

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
    """
    user = db.get(User, data.user_id)
    if user is None:
        raise AppError(
            status_code=404,
            code="USER_NOT_FOUND",
            message="No user exists with this user_id.",
        )
    if user.role != UserRole.PROVIDER:
        raise AppError(
            status_code=409,
            code="USER_NOT_A_PROVIDER",
            message="This user's role is not PROVIDER.",
        )

    existing = db.execute(
        select(Provider).where(Provider.user_id == data.user_id)
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError(
            status_code=409,
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
            status_code=404,
            code="DEPARTMENT_OR_SPECIALTY_NOT_FOUND",
            message="No department or specialty exists with the given id.",
        )
    db.refresh(provider)
    return provider


def get_provider(db: Session, provider_id: int) -> Provider:
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise AppError(
            status_code=404,
            code="PROVIDER_NOT_FOUND",
            message="No provider exists with this id.",
        )
    return provider


def list_providers(
    db: Session, pagination: PaginationParams
) -> tuple[list[Provider], int]:
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
            status_code=404,
            code="DEPARTMENT_OR_SPECIALTY_NOT_FOUND",
            message="No department or specialty exists with the given id.",
        )
    db.refresh(provider)
    return provider
