"""Separate internal bots and outbound routes from BotX connections."""
import sqlalchemy as sa
from alembic import op

from gateway.models import InternalBot, NotificationRoute

revision = "0005_gateway_topology"
down_revision = "0004_notification_integrations"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    InternalBot.__table__.create(bind, checkfirst=True)
    NotificationRoute.__table__.create(bind, checkfirst=True)
    op.execute("""
        INSERT INTO internal_bots (id, name, template_id, default_handler, botx_id)
        SELECT id, name, template_id, default_handler, id FROM bots
        ON CONFLICT DO NOTHING
    """)
    if any(c["name"] == "bot_id" for c in sa.inspect(bind).get_columns("user_grants")):
        op.drop_constraint("user_grants_bot_id_fkey", "user_grants", type_="foreignkey")
        op.alter_column("user_grants", "bot_id", new_column_name="internal_bot_id")
        op.create_foreign_key(
            "user_grants_internal_bot_id_fkey", "user_grants", "internal_bots",
            ["internal_bot_id"], ["id"],
        )
    op.alter_column("notification_tokens", "bot_id", nullable=True)
    op.execute("""
        INSERT INTO notification_routes (integration_id, bot_id, chat_ids)
        SELECT DISTINCT integration_id, bot_id, '[]'::jsonb
        FROM notification_tokens WHERE bot_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    # The old schema cannot represent tokens for a whole system or unbound bots.
    # Abort instead of discarding grants, tokens, or delivery history.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM notification_tokens WHERE bot_id IS NULL)
                OR EXISTS (SELECT 1 FROM internal_bots WHERE botx_id IS DISTINCT FROM id)
                OR EXISTS (SELECT 1 FROM notification_routes r WHERE r.chat_ids <> '[]'::jsonb
                    OR NOT EXISTS (SELECT 1 FROM notification_tokens t
                        WHERE t.integration_id = r.integration_id AND t.bot_id = r.bot_id))
                OR EXISTS (SELECT 1 FROM notification_tokens t WHERE NOT EXISTS
                    (SELECT 1 FROM notification_routes r
                        WHERE t.integration_id = r.integration_id AND t.bot_id = r.bot_id))
            THEN RAISE EXCEPTION 'Existing topology cannot be represented by schema 0004';
            END IF;
        END $$;
    """)
    op.execute("""
        UPDATE bots SET template_id = i.template_id, default_handler = i.default_handler
        FROM internal_bots i WHERE bots.id = i.botx_id
    """)
    op.alter_column("notification_tokens", "bot_id", nullable=False)
    op.drop_constraint("user_grants_internal_bot_id_fkey", "user_grants", type_="foreignkey")
    op.alter_column("user_grants", "internal_bot_id", new_column_name="bot_id")
    op.create_foreign_key("user_grants_bot_id_fkey", "user_grants", "bots", ["bot_id"], ["id"])
    op.drop_table("notification_routes")
    op.drop_table("internal_bots")
