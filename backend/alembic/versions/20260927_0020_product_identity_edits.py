"""Audit explicit human Product identity edits.

Revision ID: 20260927_0020
Revises: 20260926_0019
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "20260927_0020"
down_revision: str | None = "20260926_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_identity_edits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("old_name", sa.String(255), nullable=False),
        sa.Column("new_name", sa.String(255), nullable=False),
        sa.Column("old_brand_id", sa.Uuid(), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("new_brand_id", sa.Uuid(), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_product_identity_edits_product_id", "product_identity_edits", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_product_identity_edits_product_id", table_name="product_identity_edits")
    op.drop_table("product_identity_edits")
