"""Give every video stream its own uid

The uid column default was evaluated once at import, so streams created by the
same process shared one uid, and older rows have none. GET /Marti/api/video/<uid>
looks streams up by uid, so both cases broke CloudTAK "Edit Lease".

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-23 12:10:00.000000

"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT path, uid FROM video_streams ORDER BY path")).all()
    seen = set()
    for path, uid in rows:
        if uid and uid not in seen:
            seen.add(uid)
            continue
        conn.execute(
            sa.text("UPDATE video_streams SET uid = :uid WHERE path = :path"),
            {"uid": str(uuid.uuid4()), "path": path},
        )


def downgrade():
    pass
