"""Multi-user identities, encrypted browser sessions and durable request quotas.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.add_column(
        "users", sa.Column("status", sa.String(20), nullable=False, server_default="active")
    )
    op.create_check_constraint(
        "status_valid", "users", "status IN ('active','suspended','deleted')"
    )
    op.create_table(
        "user_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
    )
    # The only deployed pre-0003 OIDC identity is the Google CLI bootstrap owner.
    # Preserve it explicitly; never link its email to a new Auth0 identity.
    op.execute("""
        INSERT INTO user_identities (id,user_id,issuer,subject)
        SELECT gen_random_uuid(),id,'https://accounts.google.com',oidc_subject
        FROM users WHERE oidc_subject IS NOT NULL
    """)
    op.create_table(
        "portal_logins",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("encrypted_data", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "portal_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("encrypted_data", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "user_quotas",
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_portal_logins_expires_at", "portal_logins", ["expires_at"])
    op.create_index("ix_portal_sessions_expires_at", "portal_sessions", ["expires_at"])
    op.create_index("ix_user_quotas_window_start", "user_quotas", ["window_start"])


def downgrade() -> None:
    for table in ("user_quotas", "portal_sessions", "portal_logins", "user_identities"):
        op.drop_table(table)
    op.drop_constraint("status_valid", "users", type_="check")
    op.drop_column("users", "status")
    # A production downgrade is intentionally not data-destructive: this fails if
    # duplicate profile emails exist, instead of merging/deleting independent users.
    op.create_unique_constraint("uq_users_email", "users", ["email"])
