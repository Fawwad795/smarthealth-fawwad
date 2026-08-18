"""The declarative Base every ORM model inherits from.

Alembic also imports this to discover the schema when autogenerating
migrations. Note there is deliberately no `Base.metadata.create_all()`
anywhere in this project — Alembic owns the schema.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
