"""drop redundant services department index

The unique constraint on (department_id, name) already builds a composite
index with department_id as its leading column, so a lookup filtering on
department_id alone can use that index without this one. This index only
duplicated write cost with no read benefit.

Revision ID: 18cefa050a45
Revises: 975ead0186f7
Create Date: 2026-08-23 10:59:36.382761+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '18cefa050a45'
down_revision: Union[str, None] = '975ead0186f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_services_department_id', table_name='services')


def downgrade() -> None:
    op.create_index('ix_services_department_id', 'services', ['department_id'], unique=False)
