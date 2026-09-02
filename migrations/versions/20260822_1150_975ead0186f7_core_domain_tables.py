"""core domain tables

Creates the nine Week 1 tables: clinics, specialties, users, departments,
patients, providers, services, provider_schedules and slots.

Generated with --autogenerate and then reviewed line by line. Two constructs
are worth noting because they are the ones a reader will look for:

- ex_slots_no_overlap is a PostgreSQL exclusion constraint. It is what stops
  two overlapping slots existing for one provider -- something no unique
  constraint can express, since overlapping rows have different start times.
  It requires the btree_gist extension, enabled in revision e7f924b0d5be.

- The enum columns carry no visible CheckConstraint. The check is emitted by
  the Enum type itself via create_constraint=True, producing ck_users_role,
  ck_services_status and ck_slots_status.

Revision ID: 975ead0186f7
Revises: e7f924b0d5be
Create Date: 2026-08-22 11:50:11.047654+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '975ead0186f7'
down_revision: Union[str, None] = 'e7f924b0d5be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('clinics',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clinics')),
    sa.UniqueConstraint('name', name=op.f('uq_clinics_name'))
    )
    op.create_table('specialties',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_specialties')),
    sa.UniqueConstraint('name', name=op.f('uq_specialties_name'))
    )
    op.create_table('users',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('PATIENT', 'PROVIDER', 'FRONT_DESK', 'ADMIN', name='role', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index('uq_users_email_lower', 'users', [sa.text('lower(email)')], unique=True)
    op.create_table('departments',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('clinic_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('order_index', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_departments_clinic_id_clinics'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_departments')),
    sa.UniqueConstraint('clinic_id', 'name', name=op.f('uq_departments_clinic_id_name'))
    )
    op.create_table('patients',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('dob', sa.Date(), nullable=False),
    sa.Column('contact', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_patients_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_patients')),
    sa.UniqueConstraint('user_id', name=op.f('uq_patients_user_id'))
    )
    op.create_table('providers',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('department_id', sa.BigInteger(), nullable=False),
    sa.Column('specialty_id', sa.BigInteger(), nullable=False),
    sa.Column('bio', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], name=op.f('fk_providers_department_id_departments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['specialty_id'], ['specialties.id'], name=op.f('fk_providers_specialty_id_specialties'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_providers_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_providers')),
    sa.UniqueConstraint('user_id', name=op.f('uq_providers_user_id'))
    )
    op.create_index(op.f('ix_providers_department_id'), 'providers', ['department_id'], unique=False)
    op.create_index(op.f('ix_providers_specialty_id'), 'providers', ['specialty_id'], unique=False)
    op.create_table('services',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('department_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('prep_instructions', sa.Text(), nullable=True),
    sa.Column('status', sa.Enum('DRAFT', 'PUBLISHING', 'PUBLISHED', 'PUBLISH_FAILED', 'INACTIVE', name='status', native_enum=False, create_constraint=True), server_default='DRAFT', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], name=op.f('fk_services_department_id_departments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_services')),
    sa.UniqueConstraint('department_id', 'name', name=op.f('uq_services_department_id_name'))
    )
    op.create_index(op.f('ix_services_department_id'), 'services', ['department_id'], unique=False)
    op.create_index(op.f('ix_services_status'), 'services', ['status'], unique=False)
    op.create_table('provider_schedules',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('provider_id', sa.BigInteger(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.Column('slot_duration_minutes', sa.SmallInteger(), server_default=sa.text('15'), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('end_time > start_time', name=op.f('ck_provider_schedules_end_after_start')),
    sa.CheckConstraint('slot_duration_minutes > 0', name=op.f('ck_provider_schedules_slot_duration_positive')),
    sa.CheckConstraint('weekday BETWEEN 0 AND 6', name=op.f('ck_provider_schedules_weekday_range')),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], name=op.f('fk_provider_schedules_provider_id_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_schedules')),
    sa.UniqueConstraint('provider_id', 'weekday', 'start_time', name=op.f('uq_provider_schedules_provider_id_weekday_start_time'))
    )
    op.create_table('slots',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('provider_id', sa.BigInteger(), nullable=False),
    sa.Column('start_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('AVAILABLE', 'RESERVED', 'BOOKED', 'BLOCKED', name='status', native_enum=False, create_constraint=True), server_default='AVAILABLE', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    postgresql.ExcludeConstraint((sa.column('provider_id'), '='), (sa.text('tstzrange(start_time, end_time)'), '&&'), using='gist', name='ex_slots_no_overlap'),
    sa.CheckConstraint('end_time > start_time', name=op.f('ck_slots_end_after_start')),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], name=op.f('fk_slots_provider_id_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_slots')),
    sa.UniqueConstraint('provider_id', 'start_time', name=op.f('uq_slots_provider_id_start_time'))
    )
    op.create_index(op.f('ix_slots_status'), 'slots', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_slots_status'), table_name='slots')
    op.drop_table('slots')
    op.drop_table('provider_schedules')
    op.drop_index(op.f('ix_services_status'), table_name='services')
    op.drop_index(op.f('ix_services_department_id'), table_name='services')
    op.drop_table('services')
    op.drop_index(op.f('ix_providers_specialty_id'), table_name='providers')
    op.drop_index(op.f('ix_providers_department_id'), table_name='providers')
    op.drop_table('providers')
    op.drop_table('patients')
    op.drop_table('departments')
    op.drop_index('uq_users_email_lower', table_name='users')
    op.drop_table('users')
    op.drop_table('specialties')
    op.drop_table('clinics')
