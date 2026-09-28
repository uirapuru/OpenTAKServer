"""data_packages.filename is not unique

ATAK names every chat attachment chat-transfer.zip, so a unique filename
dropped every attachment after the first one.

Revision ID: b7c1d2e3f4a5
Revises: 00442761c803
"""
from alembic import op

revision = "b7c1d2e3f4a5"
down_revision = "00442761c803"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("data_packages", schema=None) as batch_op:
        batch_op.drop_constraint("data_packages_filename_key", type_="unique")


def downgrade():
    with op.batch_alter_table("data_packages", schema=None) as batch_op:
        batch_op.create_unique_constraint("data_packages_filename_key", ["filename"])
