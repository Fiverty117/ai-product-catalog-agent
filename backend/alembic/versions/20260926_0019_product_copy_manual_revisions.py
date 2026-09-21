"""Append-only human Product copy revisions.

Revision ID: 20260926_0019
Revises: 20260925_0018
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260926_0019"
down_revision: str | None = "20260925_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_copy_manual_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("copy_type", sa.String(length=17), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("copy_type IN ('short_description')", name="product_copy_manual_revision_type"),
        sa.CheckConstraint("length(trim(short_description)) > 0 AND length(short_description) <= 180", name="ck_product_copy_manual_revisions_text"),
        sa.CheckConstraint("length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-fA-F]*'", name="ck_product_copy_manual_revisions_fingerprint"),
    )
    op.create_index("ix_product_copy_manual_revisions_product_id", "product_copy_manual_revisions", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_product_copy_manual_revisions_product_id", table_name="product_copy_manual_revisions")
    op.drop_table("product_copy_manual_revisions")
