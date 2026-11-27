"""Cascade entity deletion to owned records and clear optional bindings."""

from alembic import op

revision = "0010_entity_delete_cascades"
down_revision = "0009_chat_assignment_routing"
branch_labels = None
depends_on = None


_FOREIGN_KEYS = (
    ("internal_bots", "botx_id", "bots", "SET NULL"),
    ("internal_bots", "external_system_id", "notification_integrations", "SET NULL"),
    ("chats", "bot_id", "bots", "CASCADE"),
    ("user_grants", "internal_bot_id", "internal_bots", "CASCADE"),
    ("notification_routes", "bot_id", "bots", "CASCADE"),
)


def _replace_foreign_key(table, column, target, ondelete):
    name = f"{table}_{column}_fkey"
    op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(name, table, target, [column], ["id"], ondelete=ondelete)


def upgrade():
    for table, column, target, ondelete in _FOREIGN_KEYS:
        _replace_foreign_key(table, column, target, ondelete)

    # Fresh installations may already have the route FK from current metadata.
    op.execute("ALTER TABLE notification_tokens DROP CONSTRAINT IF EXISTS notification_tokens_bot_id_fkey")
    op.execute("ALTER TABLE notification_tokens DROP CONSTRAINT IF EXISTS notification_tokens_route_fkey")
    # Older versions could leave scoped keys behind after removing a route.
    op.execute("""
        DELETE FROM notification_tokens AS token
        WHERE token.bot_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM notification_routes AS route
              WHERE route.integration_id = token.integration_id AND route.bot_id = token.bot_id
          )
    """)
    op.create_foreign_key(
        "notification_tokens_route_fkey", "notification_tokens", "notification_routes",
        ["integration_id", "bot_id"], ["integration_id", "bot_id"], ondelete="CASCADE",
    )


def downgrade():
    op.drop_constraint("notification_tokens_route_fkey", "notification_tokens", type_="foreignkey")
    op.create_foreign_key(
        "notification_tokens_bot_id_fkey", "notification_tokens", "bots", ["bot_id"], ["id"],
    )
    for table, column, target, _ in reversed(_FOREIGN_KEYS):
        _replace_foreign_key(table, column, target, None)
