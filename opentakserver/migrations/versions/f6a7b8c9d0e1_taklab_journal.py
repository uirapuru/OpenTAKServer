"""taklab: environment journal (who did what, when and where)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-24 21:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "taklab_journal",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=True),
        sa.Column("callsign", sa.String(length=255), nullable=True),
        sa.Column("device_uid", sa.String(length=255), nullable=True),
        sa.Column("object_uid", sa.String(length=255), nullable=True),
        sa.Column("object_type", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recipients", sa.Text(), nullable=True),
        sa.Column("chatroom", sa.String(length=255), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("mission", sa.String(length=255), nullable=True),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("taklab_journal_at_idx", "taklab_journal", ["at"])
    op.create_index("taklab_journal_account_idx", "taklab_journal", ["account"])
    op.create_index("taklab_journal_kind_idx", "taklab_journal", ["kind"])
    op.create_index("taklab_journal_object_uid_idx", "taklab_journal", ["object_uid"])
    op.create_index("taklab_journal_device_kind_idx", "taklab_journal", ["device_uid", "kind"])


def downgrade():
    op.drop_index("taklab_journal_device_kind_idx", table_name="taklab_journal")
    op.drop_index("taklab_journal_object_uid_idx", table_name="taklab_journal")
    op.drop_index("taklab_journal_kind_idx", table_name="taklab_journal")
    op.drop_index("taklab_journal_account_idx", table_name="taklab_journal")
    op.drop_index("taklab_journal_at_idx", table_name="taklab_journal")
    op.drop_table("taklab_journal")
