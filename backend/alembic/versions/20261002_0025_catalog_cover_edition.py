"""Add immutable cover uploads and nullable v4 render audit.

Revision ID: 20261002_0025
Revises: 20261001_0024
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261002_0025"
down_revision: str | None = "20261001_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_cover_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=32), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(trim(file_path)) > 0", name="ck_catalog_cover_assets_path"),
        sa.CheckConstraint("length(checksum_sha256) = 64 AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'", name="ck_catalog_cover_assets_checksum"),
        sa.CheckConstraint("mime_type IN ('image/png', 'image/jpeg', 'image/webp')", name="ck_catalog_cover_assets_mime"),
        sa.CheckConstraint("file_size_bytes > 0 AND width > 0 AND height > 0", name="ck_catalog_cover_assets_dimensions"),
    )
    op.create_index("ix_catalog_cover_assets_checksum", "catalog_cover_assets", ["checksum_sha256"])
    with op.batch_alter_table("catalog_render_runs") as batch:
        batch.add_column(sa.Column("cover_schema_version", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("cover_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("cover_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("catalog_render_runs") as batch:
        batch.drop_column("cover_data")
        batch.drop_column("cover_hash")
        batch.drop_column("cover_schema_version")
    op.drop_index("ix_catalog_cover_assets_checksum", table_name="catalog_cover_assets")
    op.drop_table("catalog_cover_assets")
