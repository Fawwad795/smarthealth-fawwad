"""Column definitions shared by every model.

A mixin is not a table. It never inherits from Base, so no table is created
for it and nothing is joined at the database level. SQLAlchemy's declarative
system walks a model's parent classes, finds the column definitions below and
copies them into that model's own table — so every table ends up with its own
pair of timestamp columns, defined once here.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """UTC-aware created_at / updated_at for any model that inherits it."""

    created_at: Mapped[datetime] = mapped_column(
        # timezone=True makes this TIMESTAMPTZ: an actual moment, not a bare
        # wall-clock reading. Without it, two rows written by processes on
        # different clocks compare as if they were on the same one.
        DateTime(timezone=True),
        # Postgres fills this in, not Python. By Week 3 four processes write
        # rows (API, Celery, Temporal, consumer) and ordering them needs one
        # clock; a server default also covers seed scripts and psql inserts,
        # which never run our Python.
        server_default=func.now(),
        nullable=False,
        # Keep the timestamps at the end of the table. Mixin columns are
        # otherwise placed before the model's own columns, so `id` would not
        # be first when reading the table.
        sort_order=100,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        # onupdate is a SQLAlchemy-side mechanic: it appends
        # `updated_at = now()` to UPDATE statements the ORM builds. Postgres
        # has no on-update column default, so a raw UPDATE — such as the
        # atomic slot reservation in Week 2 — must set this column itself.
        onupdate=func.now(),
        nullable=False,
        sort_order=101,
    )
