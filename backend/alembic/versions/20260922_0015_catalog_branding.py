"""Add current catalog publisher profiles and frozen render branding.

Revision ID: 20260922_0015
Revises: 20260921_0014
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260922_0015"
down_revision: str | None = "20260921_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_brand_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(file_path)) > 0", name="ck_catalog_brand_assets_path"),
        sa.CheckConstraint("length(checksum_sha256) = 64 AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'", name="ck_catalog_brand_assets_checksum"),
        sa.CheckConstraint("mime_type IN ('image/png', 'image/jpeg', 'image/webp')", name="ck_catalog_brand_assets_mime"),
        sa.CheckConstraint("file_size_bytes > 0 AND width > 0 AND height > 0", name="ck_catalog_brand_assets_dimensions"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_catalog_brand_assets_checksum", "catalog_brand_assets", ["checksum_sha256"])
    op.create_table(
        "catalog_brand_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("logo_asset_id", sa.Uuid(), nullable=True),
        sa.Column("primary_color", sa.String(7), nullable=False),
        sa.Column("accent_color", sa.String(7), nullable=False),
        sa.Column("contact_text", sa.String(500), nullable=True),
        sa.Column("social_handle", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_catalog_brand_profiles_key"),
        sa.CheckConstraint("key GLOB '[a-z]*' AND key NOT GLOB '*[^a-z0-9-]*' AND key NOT GLOB '*--*' AND key NOT GLOB '*-'", name="ck_catalog_brand_profiles_key_slug"),
        sa.CheckConstraint("length(trim(display_name)) > 0", name="ck_catalog_brand_profiles_name"),
        sa.CheckConstraint("length(primary_color) = 7 AND primary_color GLOB '#[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'", name="ck_catalog_brand_profiles_primary_color"),
        sa.CheckConstraint("length(accent_color) = 7 AND accent_color GLOB '#[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'", name="ck_catalog_brand_profiles_accent_color"),
        sa.ForeignKeyConstraint(["logo_asset_id"], ["catalog_brand_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_catalog_brand_profiles_key"),
    )
    op.create_index("ix_catalog_brand_profiles_logo_asset_id", "catalog_brand_profiles", ["logo_asset_id"])
    # SQLite can add nullable columns without rebuilding the referenced run table.
    # A batch copy/drop would fail when historical catalog_artifacts FK to runs.
    # Alembic's add_column attempts a separate ALTER CONSTRAINT on SQLite.
    # SQLite itself accepts a nullable REFERENCES column in one additive ALTER.
    op.execute("ALTER TABLE catalog_render_runs ADD COLUMN catalog_brand_profile_id CHAR(32) REFERENCES catalog_brand_profiles(id)")
    op.add_column("catalog_render_runs", sa.Column("branding_schema_version", sa.String(100), nullable=True))
    op.add_column("catalog_render_runs", sa.Column("branding_hash", sa.String(64), nullable=True))
    op.add_column("catalog_render_runs", sa.Column("branding_data", sa.JSON(), nullable=True))
    op.create_index("ix_catalog_render_runs_brand_profile_id", "catalog_render_runs", ["catalog_brand_profile_id"])


def downgrade() -> None:
    op.drop_index("ix_catalog_render_runs_brand_profile_id", table_name="catalog_render_runs")
    op.drop_column("catalog_render_runs", "branding_data")
    op.drop_column("catalog_render_runs", "branding_hash")
    op.drop_column("catalog_render_runs", "branding_schema_version")
    op.drop_column("catalog_render_runs", "catalog_brand_profile_id")
    op.drop_index("ix_catalog_brand_profiles_logo_asset_id", table_name="catalog_brand_profiles")
    op.drop_table("catalog_brand_profiles")
    op.drop_index("ix_catalog_brand_assets_checksum", table_name="catalog_brand_assets")
    op.drop_table("catalog_brand_assets")
