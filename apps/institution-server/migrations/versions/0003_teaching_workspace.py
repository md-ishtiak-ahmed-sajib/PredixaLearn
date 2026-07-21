"""Teaching workspace catalog indexes.

Revision ID: 0003_teaching_workspace
Revises: 0002_api_clients_worker_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_teaching_workspace"
down_revision = "0002_api_clients_worker_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("resources")}
    if "ix_resources_tenant_type_status_course" not in indexes:
        op.create_index(
            "ix_resources_tenant_type_status_course",
            "resources",
            ["tenant_id", "resource_type", "status", "course_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("resources")}
    if "ix_resources_tenant_type_status_course" in indexes:
        op.drop_index("ix_resources_tenant_type_status_course", table_name="resources")
