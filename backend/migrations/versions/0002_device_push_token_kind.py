"""device push token kind

FCM is migrating from registration tokens to Firebase Installation IDs (FIDs). Tokens
stored before this revision are registration tokens; new registrations default to FIDs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28 22:07:06.694011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "push_token_kind",
            sa.String(length=20),
            server_default="registration_token",
            nullable=False,
        ),
    )
    op.alter_column("devices", "push_token_kind", server_default="fid")
    op.create_check_constraint(
        op.f("ck_devices_push_token_kind_valid"),
        "devices",
        "push_token_kind IN ('fid', 'registration_token')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_devices_push_token_kind_valid"), "devices", type_="check")
    op.drop_column("devices", "push_token_kind")
