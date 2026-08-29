"""The declarative Base every ORM model inherits from.

Alembic also imports this to discover the schema when autogenerating
migrations. Note there is deliberately no `Base.metadata.create_all()`
anywhere in this project — Alembic owns the schema.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Every constraint and index in Postgres is a named object. If we don't name
# them, Postgres invents names inconsistently (`slots_provider_id_fkey`,
# `users_email_key`, `..._key1` on a collision). A later migration that drops
# one has to state its exact name — so an invented name means hardcoding a
# string discovered by inspecting one particular database, which may not match
# the name on someone else's. These templates make every name derivable from a
# rule instead, so migrations are portable and autogenerate diffs are accurate.
#
# Placeholders SQLAlchemy fills in:
#   %(table_name)s - the table name
#   %(column_0_name)s - the first column name
#   %(column_0_N_name)s     all involved columns, joined with underscores
#   %(referred_table_name)s for a foreign key, the table it points at
#   %(constraint_name)s     the name passed explicitly to CheckConstraint(...)
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",  # index
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",  # unique constraint
    "ck": "ck_%(table_name)s_%(constraint_name)s",  # check constraint
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",  # primary key
}


class Base(DeclarativeBase):
    """The declarative base all ORM models inherit from.

    Its only job is to own a MetaData carrying NAMING_CONVENTION above, so
    every table registered against it gets constraint and index names that
    follow one rule rather than whatever Postgres happens to invent.
    """

    # DeclarativeBase would create a MetaData for us silently. We create it
    # ourselves so the naming convention above is attached to it — every table
    # that inherits from Base registers in this MetaData and inherits the rules.
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
