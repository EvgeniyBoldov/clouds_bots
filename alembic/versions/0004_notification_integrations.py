"""Store notification integrations, token digests and delivery attribution."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from gateway.models import NotificationIntegration

revision = "0004_notification_integrations"
down_revision = "0003_bot_templates"
branch_labels = None
depends_on = None


def upgrade():
    # The initial revision uses current metadata when installing a fresh database.
    bind = op.get_bind()
    NotificationIntegration.__table__.create(bind, checkfirst=True)
    if not sa.inspect(bind).has_table("notification_tokens"):
        # The route table does not exist yet on databases upgrading through 0004.
        op.create_table(
            "notification_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("integration_id", sa.Integer(), sa.ForeignKey("notification_integrations.id"), nullable=False),
            sa.Column("bot_id", UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False),
            sa.Column("digest", sa.String(64), nullable=False, unique=True),
            sa.Column("prefix", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_notification_tokens_integration_id", "notification_tokens", ["integration_id"])
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS notification_token_id INTEGER "
        "REFERENCES notification_tokens(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_jobs_notification_token_id_id "
        "ON jobs (notification_token_id, id)"
    )


def downgrade():
    op.drop_index("ix_jobs_notification_token_id_id", table_name="jobs")
    op.drop_column("jobs", "notification_token_id")
    op.drop_table("notification_tokens")
    op.drop_table("notification_integrations")
