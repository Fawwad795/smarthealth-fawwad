"""enable btree_gist extension

Revision ID: e7f924b0d5be
Revises: a589f9dbe585
Create Date: 2026-08-22 11:35:56.506835+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7f924b0d5be'
down_revision: Union[str, None] = 'a589f9dbe585'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Exclusion constraints are backed by GiST indexes. GiST natively handles
    # "fuzzy" operators such as && (range overlap), but not plain equality on
    # a scalar like bigint -- that is B-tree's job. btree_gist bridges the
    # two, so slots can exclude on `provider_id WITH =` and
    # `tstzrange(start_time, end_time) WITH &&` in one index.
    #
    # IF NOT EXISTS because the extension may already be present from manual
    # work or another project in the same cluster, and a migration that fails
    # on someone else's machine for that reason is worse than a no-op.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")


def downgrade() -> None:
    # Deliberately no CASCADE. This will fail while the slots table still
    # exists, because the exclusion constraint's index depends on the
    # extension -- and that failure is correct. CASCADE would silently drop
    # the exclusion constraint along with the extension and report success,
    # removing the only guarantee that a provider cannot be double-booked.
    #
    # The migration chain already handles the ordering: `downgrade base` runs
    # downgrades in reverse, so the tables migration drops slots first.
    op.execute("DROP EXTENSION IF EXISTS btree_gist;")

