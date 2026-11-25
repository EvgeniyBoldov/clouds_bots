"""Configure HTTPS connections from internal bots to external systems."""
import sqlalchemy as sa
from alembic import op

revision = "0006_external_system_connections"
down_revision = "0005_gateway_topology"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh databases already have the columns through the current ORM metadata.
    inspector = sa.inspect(op.get_bind())
    integration_columns = {column["name"] for column in inspector.get_columns("notification_integrations")}
    for column in (
        sa.Column("outbound_url", sa.String(2048)),
        sa.Column("outbound_auth_type", sa.String(16), nullable=False, server_default="none"),
        sa.Column("outbound_token", sa.Text()),
    ):
        if column.name not in integration_columns:
            op.add_column("notification_integrations", column)

    if "external_system_id" not in {column["name"] for column in inspector.get_columns("internal_bots")}:
        op.add_column("internal_bots", sa.Column("external_system_id", sa.Integer()))
        op.create_foreign_key(
            "internal_bots_external_system_id_fkey", "internal_bots", "notification_integrations",
            ["external_system_id"], ["id"],
        )


def downgrade():
    op.drop_constraint("internal_bots_external_system_id_fkey", "internal_bots", type_="foreignkey")
    op.drop_column("internal_bots", "external_system_id")
    for name in ("outbound_token", "outbound_auth_type", "outbound_url"):
        op.drop_column("notification_integrations", name)
