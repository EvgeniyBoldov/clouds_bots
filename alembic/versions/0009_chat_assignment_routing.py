"""Use canonical personal chats and local assignment IDs."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0009_chat_assignment_routing"
down_revision = "0008_integration_delete_cascade"
branch_labels = None
depends_on = None

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "user_huid" not in {c["name"] for c in inspector.get_columns("chats")}:
        _upgrade_chats()
    if "purpose" not in {c["name"] for c in inspector.get_columns("notification_tokens")}:
        op.add_column("notification_tokens", sa.Column(
            "purpose", sa.String(24), nullable=False, server_default="notification",
        ))

    # The initial revision and 0007 can already create the current ORM schema.
    if "request_id" not in {c["name"] for c in inspector.get_columns("kopatych_runs")}:
        op.add_column("kopatych_runs", sa.Column("request_id", UUID(as_uuid=True)))
        op.execute("UPDATE kopatych_runs SET request_id = gen_random_uuid()")
        pk = inspector.get_pk_constraint("kopatych_runs")["name"]
        op.drop_constraint(pk, "kopatych_runs", type_="primary")
        op.alter_column("kopatych_runs", "run_id", nullable=True)
        op.create_primary_key("kopatych_runs_pkey", "kopatych_runs", ["request_id"])
        op.create_unique_constraint("uq_kopatych_system_run", "kopatych_runs", ["external_system_id", "run_id"])
        op.add_column("kopatych_runs", sa.Column("status", sa.String(32), nullable=False, server_default="pending"))
        op.add_column("kopatych_runs", sa.Column("delivery_mode", sa.String(8), nullable=False, server_default="poll"))
        op.execute("UPDATE kopatych_runs SET status = CASE WHEN terminal THEN 'closed' ELSE 'queued' END")
        op.add_column("kopatych_runs", sa.Column("callback_sequence", sa.Integer(), nullable=False, server_default="0"))
        op.add_column("kopatych_runs", sa.Column("identity_mail", sa.String(320)))

    if not inspector.has_table("assignment_events"):
        op.create_table(
            "assignment_events",
            sa.Column("request_id", UUID(as_uuid=True), sa.ForeignKey("kopatych_runs.request_id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("payload", JSONB(), nullable=False),
            sa.Column("delivered_message_id", UUID(as_uuid=True)),
            sa.UniqueConstraint("request_id", "sequence"),
        )


def _upgrade_chats():
    op.add_column("chats", sa.Column("user_huid", UUID(as_uuid=True)))
    op.add_column("chats", sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("chats", sa.Column("seen_at", sa.DateTime(timezone=True)))
    op.get_bind().execute(sa.text("""
        WITH personal_events AS (
            SELECT DISTINCT ON (bot_id, chat_id) bot_id, chat_id, id,
                   CAST(payload->>'sender_id' AS uuid) AS user_huid
            FROM inbound_events
            WHERE lower(payload->>'chat_type') IN ('chat', 'personal', 'personal_chat', 'chattypes.personal_chat')
              AND payload->>'sender_id' ~* :uuid_pattern
            ORDER BY bot_id, chat_id, id DESC
        ),
        personal_chats AS (
            SELECT c.id, c.bot_id, e.id AS event_id, coalesce(e.user_huid, CASE
                    WHEN c.settings->>'user_huid' ~* :uuid_pattern
                    THEN CAST(c.settings->>'user_huid' AS uuid) END) AS user_huid
            FROM chats c LEFT JOIN personal_events e USING (bot_id, chat_id)
            WHERE e.id IS NOT NULL OR lower(c.chat_type) IN ('chat', 'personal', 'personal_chat', 'chattypes.personal_chat')
        ),
        ranked AS (
            SELECT id, user_huid, row_number() OVER (
                PARTITION BY bot_id, user_huid ORDER BY event_id DESC NULLS LAST, id DESC
            ) AS rank
            FROM personal_chats WHERE user_huid IS NOT NULL
        )
        UPDATE chats c SET chat_type = 'personal', user_huid = r.user_huid, available = (r.rank = 1)
        FROM ranked r WHERE c.id = r.id
    """), {"uuid_pattern": _UUID_PATTERN})
    op.create_index(
        "uq_chats_active_personal", "chats", ["bot_id", "user_huid"], unique=True,
        postgresql_where=sa.text("available AND chat_type = 'personal'"),
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM kopatych_runs WHERE run_id IS NULL OR delivery_mode = 'push')
                OR EXISTS (SELECT run_id FROM kopatych_runs GROUP BY run_id HAVING count(*) > 1)
                OR EXISTS (SELECT FROM notification_tokens WHERE purpose <> 'notification')
            THEN RAISE EXCEPTION 'Existing assignment or token data cannot be represented by schema 0008';
            END IF;
        END $$;
    """)
    op.drop_table("assignment_events")
    op.drop_constraint("kopatych_runs_pkey", "kopatych_runs", type_="primary")
    op.drop_constraint("uq_kopatych_system_run", "kopatych_runs", type_="unique")
    op.create_primary_key("kopatych_runs_pkey", "kopatych_runs", ["run_id"])
    for name in ("identity_mail", "callback_sequence", "delivery_mode", "status", "request_id"):
        op.drop_column("kopatych_runs", name)
    op.drop_column("notification_tokens", "purpose")
    op.execute("""
        UPDATE chats SET settings = settings || jsonb_build_object('user_huid', user_huid::text)
        WHERE chat_type = 'personal' AND user_huid IS NOT NULL
    """)
    op.drop_index("uq_chats_active_personal", table_name="chats")
    for name in ("seen_at", "available", "user_huid"):
        op.drop_column("chats", name)
