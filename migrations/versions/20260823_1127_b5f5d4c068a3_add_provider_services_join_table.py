"""add provider_services join table

Explicitly states which providers are qualified to deliver which services,
rather than inferring it from providers.department_id == services.department_id
-- a department groups people and offerings, it does not certify who does what.

Generated with --autogenerate and reviewed line by line.

Revision ID: b5f5d4c068a3
Revises: 18cefa050a45
Create Date: 2026-08-23 11:27:05.271867+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5f5d4c068a3'
down_revision: Union[str, None] = '18cefa050a45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('provider_services',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('provider_id', sa.BigInteger(), nullable=False),
    sa.Column('service_id', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], name=op.f('fk_provider_services_provider_id_providers'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_provider_services_service_id_services'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_services')),
    sa.UniqueConstraint('provider_id', 'service_id', name=op.f('uq_provider_services_provider_id_service_id'))
    )
    op.create_index(op.f('ix_provider_services_service_id'), 'provider_services', ['service_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_provider_services_service_id'), table_name='provider_services')
    op.drop_table('provider_services')
