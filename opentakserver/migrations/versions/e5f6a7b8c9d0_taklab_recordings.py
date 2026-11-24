"""taklab: recordings registry (video, voice messages, OCR scans)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-24 18:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "taklab_recordings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=True),
        sa.Column("callsign", sa.String(length=255), nullable=True),
        sa.Column("device_uid", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("track", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mime", sa.String(length=100), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("source_path", sa.String(length=255), nullable=True),
        sa.Column("segments", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="ready"),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("marker_uid", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_ref"),
    )
    op.create_index("taklab_recordings_started_idx", "taklab_recordings", ["started_at"])
    op.create_index("taklab_recordings_account_idx", "taklab_recordings", ["account"])
    op.create_index("taklab_recordings_kind_idx", "taklab_recordings", ["kind"])
    op.create_index(
        "taklab_recordings_source_path_idx", "taklab_recordings", ["source_path", "state"]
    )


def downgrade():
    op.drop_index("taklab_recordings_source_path_idx", table_name="taklab_recordings")
    op.drop_index("taklab_recordings_kind_idx", table_name="taklab_recordings")
    op.drop_index("taklab_recordings_account_idx", table_name="taklab_recordings")
    op.drop_index("taklab_recordings_started_idx", table_name="taklab_recordings")
    op.drop_table("taklab_recordings")
