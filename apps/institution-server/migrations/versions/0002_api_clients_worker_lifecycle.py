"""OAuth clients and drainable OCR workers.

Revision ID: 0002_api_clients_worker_lifecycle
Revises: 0001_institution_foundation
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_api_clients_worker_lifecycle"
down_revision = "0001_institution_foundation"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # Alembic creates ``alembic_version.version_num`` as VARCHAR(32) by
    # default. This revision identifier is longer, so PostgreSQL must widen
    # the column before Alembic records this migration as the current head.
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=64),
            existing_nullable=False,
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "api_clients" not in tables:
        op.create_table(
            "api_clients",
            sa.Column("client_id", sa.String(length=300), primary_key=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("secret_salt", sa.String(length=64), nullable=False),
            sa.Column("secret_hash", sa.String(length=128), nullable=False),
            sa.Column("permissions", sa.JSON(), nullable=False),
            sa.Column("academic_unit_ids", sa.JSON(), nullable=False),
            sa.Column("course_ids", sa.JSON(), nullable=False),
            sa.Column("enabled_features", sa.JSON(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_api_clients_tenant_id", "api_clients", ["tenant_id"])
        op.create_index(
            "ix_api_client_tenant_active", "api_clients", ["tenant_id", "active"]
        )

    worker_columns = _columns("worker_devices")
    if "status" not in worker_columns:
        op.add_column(
            "worker_devices",
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        )
    if "version" not in worker_columns:
        op.add_column(
            "worker_devices",
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE api_clients ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE api_clients FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS api_clients_tenant_isolation ON api_clients")
        op.execute(
            "CREATE POLICY api_clients_tenant_isolation ON api_clients "
            "USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS api_clients_tenant_isolation ON api_clients")
    tables = set(sa.inspect(bind).get_table_names())
    if "api_clients" in tables:
        op.drop_table("api_clients")
    worker_columns = _columns("worker_devices")
    if "version" in worker_columns:
        op.drop_column("worker_devices", "version")
    if "status" in worker_columns:
        op.drop_column("worker_devices", "status")
