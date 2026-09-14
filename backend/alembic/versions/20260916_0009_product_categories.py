"""Add configurable Categories and canonical Product assignments.

Revision ID: 20260916_0009
Revises: 20260915_0008
Create Date: 2026-09-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0009"
down_revision: str | None = "20260915_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("identity_key", sa.String(length=512), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_categories_name_nonempty",
        ),
        sa.CheckConstraint(
            "length(identity_key) > 0",
            name="ck_categories_identity_key_nonempty",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_categories_sort_order_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_categories_identity_key"),
    )
    op.create_index(
        "ix_categories_active_order",
        "categories",
        ["is_active", "sort_order", "identity_key"],
    )
    op.create_table(
        "product_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "human",
                "model",
                "ocr",
                "rule",
                "import",
                name="field_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id",
            "category_id",
            name="uq_product_categories_product_category",
        ),
    )
    op.create_index(
        "ix_product_categories_category_id",
        "product_categories",
        ["category_id"],
    )
    op.create_index(
        "uq_product_categories_one_primary",
        "product_categories",
        ["product_id"],
        unique=True,
        sqlite_where=sa.text("is_primary = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_product_categories_one_primary",
        table_name="product_categories",
    )
    op.drop_index(
        "ix_product_categories_category_id",
        table_name="product_categories",
    )
    op.drop_table("product_categories")
    op.drop_index("ix_categories_active_order", table_name="categories")
    op.drop_table("categories")
