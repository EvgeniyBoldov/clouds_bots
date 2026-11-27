"""Cascade integration deletion to tokens and routes, keep jobs."""
from alembic import op

revision = "0008_integration_delete_cascade"
down_revision = "0007_kopatych_runs"
branch_labels = None
depends_on = None


# (table, constraint, source column, target table, ondelete)
_FK_SPECS = (
    ("notification_tokens", "notification_tokens_integration_id_fkey",
     "integration_id", "notification_integrations", "CASCADE"),
    ("notification_routes", "notification_routes_integration_id_fkey",
     "integration_id", "notification_integrations", "CASCADE"),
    ("jobs", "jobs_notification_token_id_fkey",
     "notification_token_id", "notification_tokens", "SET NULL"),
)


def upgrade():
    # Fresh databases create the tables through current ORM metadata and may
    # already carry these actions; replace the constraints either way.
    for table, constraint, column, target, ondelete in _FK_SPECS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
        op.create_foreign_key(
            constraint, table, target,
            [column], ["id"],
            ondelete=ondelete,
        )


def downgrade():
    for table, constraint, column, target, _ in _FK_SPECS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
        op.create_foreign_key(
            constraint, table, target,
            [column], ["id"],
        )
