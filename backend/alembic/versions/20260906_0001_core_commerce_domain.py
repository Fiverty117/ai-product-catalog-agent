"""Create core commerce domain tables.

Revision ID: 20260906_0001
Revises:
Create Date: 2026-09-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260906_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_brands_name_nonempty"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_products_name_nonempty"),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_products_brand_id", "products", ["brand_id"])
    op.create_table(
        "skus",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("external_sku", sa.String(length=255), nullable=True),
        sa.Column("flavor", sa.String(length=255), nullable=True),
        sa.Column("size_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("size_unit", sa.String(length=32), nullable=True),
        sa.Column("servings", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size_value IS NULL OR size_value > 0", name="ck_skus_size_value_positive"),
        sa.CheckConstraint("servings IS NULL OR servings > 0", name="ck_skus_servings_positive"),
        sa.CheckConstraint(
            "(size_value IS NULL AND size_unit IS NULL) OR "
            "(size_value IS NOT NULL AND size_unit IS NOT NULL "
            "AND length(trim(size_unit)) > 0)",
            name="ck_skus_size_value_unit_pair",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skus_product_id", "skus", ["product_id"])
    op.create_table(
        "photos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "front", "back", "side", "nutrition", "other",
                name="photo_role", native_enum=False, create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("is_original", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(file_path)) > 0", name="ck_photos_file_path_nonempty"),
        sa.CheckConstraint(
            "length(checksum_sha256) = 64 "
            "AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_photos_checksum_sha256_format",
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_photos_sku_id", "photos", ["sku_id"])
    op.create_table(
        "prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_prices_amount_nonnegative"),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency) "
            "AND currency NOT GLOB '*[^A-Z]*'",
            name="ck_prices_currency_iso_code",
        ),
        sa.CheckConstraint("length(trim(source)) > 0", name="ck_prices_source_nonempty"),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_prices_sku_id_valid_from", "prices", ["sku_id", "valid_from"])


def downgrade() -> None:
    op.drop_index("ix_prices_sku_id_valid_from", table_name="prices")
    op.drop_table("prices")
    op.drop_index("ix_photos_sku_id", table_name="photos")
    op.drop_table("photos")
    op.drop_index("ix_skus_product_id", table_name="skus")
    op.drop_table("skus")
    op.drop_index("ix_products_brand_id", table_name="products")
    op.drop_table("products")
    op.drop_table("brands")
