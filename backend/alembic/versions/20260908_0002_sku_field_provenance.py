"""Add SKU field provenance metadata.

Revision ID: 20260908_0002
Revises: 20260906_0001
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260908_0002"
down_revision: str | None = "20260906_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sku_field_provenance",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column(
            "field_name",
            sa.Enum(
                "external_sku",
                "flavor",
                "size_value",
                "size_unit",
                "servings",
                name="sku_field_name",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
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
        sa.Column("confidence", sa.Numeric(precision=7, scale=6), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "extracted",
                "verified",
                "not_legible",
                "not_present",
                "failed",
                name="field_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_sku_field_provenance_confidence_range",
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sku_id",
            "field_name",
            name="uq_sku_field_provenance_sku_field",
        ),
    )
    op.create_index(
        "ix_sku_field_provenance_sku_id",
        "sku_field_provenance",
        ["sku_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sku_field_provenance_sku_id",
        table_name="sku_field_provenance",
    )
    op.drop_table("sku_field_provenance")
