"""Institutional resources, audit chain, workers, LTI, and RLS foundation."""

from alembic import op

from institution_server import models  # noqa: F401
from institution_server.database import Base

revision = "0001_institution_foundation"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "resources",
    "audit_events",
    "idempotency_records",
    "worker_devices",
    "learner_identity_mappings",
)

# Webhook deliveries are an internal dispatch queue. The dispatcher must be
# able to discover due work across tenants before it sets the tenant context;
# tenant-owned subscription and audit rows remain protected by RLS and are
# loaded only after that context is set.


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS search_document tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title, '') || ' ' || "
        "coalesce(search_text, ''))) STORED"
    )
    op.execute("ALTER TABLE resources ADD COLUMN IF NOT EXISTS embedding vector(128)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_resources_search_document "
        "ON resources USING GIN (search_document)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_resources_title_trgm "
        "ON resources USING GIN (title gin_trgm_ops)"
    )
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )
    op.execute(
        "COMMENT ON TABLE lti_platforms IS "
        "'Non-secret LTI discovery records; never exposed through generic APIs'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in reversed(TENANT_TABLES):
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    Base.metadata.drop_all(bind)
