"""Store portal runs and message delivery cursors for Kopatych."""
from alembic import op

from gateway.models import KopatychRun

revision = "0007_kopatych_runs"
down_revision = "0006_external_system_connections"
branch_labels = None
depends_on = None


def upgrade():
    KopatychRun.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    op.drop_table("kopatych_runs")
