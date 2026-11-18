"""Persist BotX credentials and resolved grant identities."""
from alembic import op


revision = "0002_dynamic_bot_accounts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # 0001_initial creates from current metadata rather than a frozen schema,
    # so the IF NOT EXISTS clauses also cover brand-new databases.
    op.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS cts_url VARCHAR(500)")
    op.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS secret_key TEXT")
    op.execute(
        "ALTER TABLE bots ADD COLUMN IF NOT EXISTS credential_revision INTEGER NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS identity_type VARCHAR(16) NOT NULL DEFAULT 'huid'"
    )
    op.execute("ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS identity_value VARCHAR(320)")
    op.execute("ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS ad_domain VARCHAR(255)")
    op.execute("UPDATE user_grants SET identity_value = user_id WHERE identity_value IS NULL")


def downgrade():
    op.execute("ALTER TABLE user_grants DROP COLUMN IF EXISTS ad_domain")
    op.execute("ALTER TABLE user_grants DROP COLUMN IF EXISTS identity_value")
    op.execute("ALTER TABLE user_grants DROP COLUMN IF EXISTS identity_type")
    op.execute("ALTER TABLE bots DROP COLUMN IF EXISTS credential_revision")
    op.execute("ALTER TABLE bots DROP COLUMN IF EXISTS secret_key")
    op.execute("ALTER TABLE bots DROP COLUMN IF EXISTS cts_url")
