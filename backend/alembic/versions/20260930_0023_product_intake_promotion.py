"""Add one immutable promotion identity per Product Intake Item.

Revision ID: 20260930_0023
Revises: 20260929_0022
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260930_0023"
down_revision: str | None = "20260929_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_intake_promotions",
        sa.Column("intake_item_id", sa.Uuid(), sa.ForeignKey("product_intake_items.id"), primary_key=True),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("brand_reused", sa.Boolean(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_product_intake_promotions_key"),
        sa.UniqueConstraint("product_id", name="uq_product_intake_promotions_product"),
    )


def downgrade() -> None:
    op.drop_table("product_intake_promotions")
