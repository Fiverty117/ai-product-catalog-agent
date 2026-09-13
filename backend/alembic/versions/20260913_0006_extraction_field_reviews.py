"""Add extraction field reviews and accepted-model provenance lineage.

Revision ID: 20260913_0006
Revises: 20260912_0005
Create Date: 2026-09-13
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_0006"
down_revision: str | None = "20260912_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sku_field_provenance") as batch_op:
        batch_op.add_column(
            sa.Column("extraction_run_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_sku_field_provenance_extraction_run_id",
            "extraction_runs",
            ["extraction_run_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_sku_field_provenance_extraction_run_id",
            ["extraction_run_id"],
        )

    op.create_table(
        "extraction_field_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column(
            "field_key",
            sa.Enum(
                "flavor",
                "size",
                "servings",
                name="extraction_review_field",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "accepted",
                "corrected",
                "rejected",
                name="extraction_review_decision",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "corrected_value",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(decision = 'corrected' AND corrected_value IS NOT NULL) OR "
            "(decision IN ('accepted', 'rejected') AND corrected_value IS NULL)",
            name="ck_extraction_field_reviews_corrected_value",
        ),
        sa.CheckConstraint(
            "(decision = 'rejected' AND applied_at IS NULL) OR "
            "(decision IN ('accepted', 'corrected') AND applied_at IS NOT NULL)",
            name="ck_extraction_field_reviews_applied_at",
        ),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"]),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_run_id",
            "sku_id",
            "field_key",
            name="uq_extraction_field_reviews_run_sku_field",
        ),
    )
    op.create_index(
        "ix_extraction_field_reviews_extraction_run_id",
        "extraction_field_reviews",
        ["extraction_run_id"],
    )
    op.create_index(
        "ix_extraction_field_reviews_sku_id",
        "extraction_field_reviews",
        ["sku_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_field_reviews_sku_id",
        table_name="extraction_field_reviews",
    )
    op.drop_index(
        "ix_extraction_field_reviews_extraction_run_id",
        table_name="extraction_field_reviews",
    )
    op.drop_table("extraction_field_reviews")

    with op.batch_alter_table("sku_field_provenance") as batch_op:
        batch_op.drop_index("ix_sku_field_provenance_extraction_run_id")
        batch_op.drop_constraint(
            "fk_sku_field_provenance_extraction_run_id",
            type_="foreignkey",
        )
        batch_op.drop_column("extraction_run_id")
