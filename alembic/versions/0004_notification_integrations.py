"""Store notification integrations, token digests and delivery attribution."""
from alembic import op

from gateway.models import NotificationIntegration, NotificationToken

revision = "0004_notification_integrations"
down_revision = "0003_bot_templates"
branch_labels = None
depends_on = None


def upgrade():
    # The initial revision uses current metadata when installing a fresh database.
    bind = op.get_bind()
    NotificationIntegration.__table__.create(bind, checkfirst=True)
    NotificationToken.__table__.create(bind, checkfirst=True)
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
