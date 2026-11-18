"""Bind BotX accounts to code-defined behavior templates."""
from alembic import op


revision = "0003_bot_templates"
down_revision = "0002_dynamic_bot_accounts"
branch_labels = None
depends_on = None


def upgrade():
    # The initial revision creates tables from the current metadata.
    op.execute(
        "ALTER TABLE bots ADD COLUMN IF NOT EXISTS template_id VARCHAR(64) NOT NULL DEFAULT 'echo'"
    )


def downgrade():
    op.execute("ALTER TABLE bots DROP COLUMN IF EXISTS template_id")
