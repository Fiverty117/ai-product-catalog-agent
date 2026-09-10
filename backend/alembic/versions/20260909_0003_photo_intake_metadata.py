"""Allow unassigned photos and add original intake metadata.

Revision ID: 20260909_0003
Revises: 20260908_0002
Create Date: 2026-09-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260909_0003"
down_revision: str | None = "20260908_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("photos") as batch_op:
        batch_op.alter_column(
            "sku_id",
            existing_type=sa.Uuid(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column("original_filename", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("mime_type", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("file_size_bytes", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("height", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_photos_original_filename_nonempty",
            "length(trim(original_filename)) > 0",
        )
        batch_op.create_check_constraint(
            "ck_photos_supported_mime_type",
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')",
        )
        batch_op.create_check_constraint(
            "ck_photos_file_size_positive",
            "file_size_bytes > 0",
        )
        batch_op.create_check_constraint("ck_photos_width_positive", "width > 0")
        batch_op.create_check_constraint("ck_photos_height_positive", "height > 0")


def downgrade() -> None:
    with op.batch_alter_table("photos") as batch_op:
        batch_op.drop_constraint(
            "ck_photos_height_positive",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_photos_width_positive",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_photos_file_size_positive",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_photos_supported_mime_type",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_photos_original_filename_nonempty",
            type_="check",
        )
        batch_op.drop_column("height")
        batch_op.drop_column("width")
        batch_op.drop_column("file_size_bytes")
        batch_op.drop_column("mime_type")
        batch_op.drop_column("original_filename")
        batch_op.alter_column(
            "sku_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
