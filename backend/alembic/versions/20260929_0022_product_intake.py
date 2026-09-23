"""Add non-canonical product intake workspace.

Revision ID: 20260929_0022
Revises: 20260928_0021
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260929_0022"
down_revision: str | None = "20260928_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_intake_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("human_edited", sa.Boolean(), nullable=False),
        sa.Column("draft_source_run_id", sa.Uuid(), sa.ForeignKey("extraction_runs.id")),
        sa.Column("latest_job_id", sa.Uuid(), sa.ForeignKey("jobs.id")),
        sa.Column("latest_run_id", sa.Uuid(), sa.ForeignKey("extraction_runs.id")),
        sa.Column("latest_action_key", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_product_intake_items_updated_at", "product_intake_items", ["updated_at", "id"])
    op.create_table(
        "product_intake_photos",
        sa.Column("intake_item_id", sa.Uuid(), sa.ForeignKey("product_intake_items.id"), primary_key=True),
        sa.Column("photo_id", sa.Uuid(), sa.ForeignKey("photos.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("intake_item_id", "position"),
        sa.UniqueConstraint("photo_id"),
    )


def downgrade() -> None:
    op.drop_table("product_intake_photos")
    op.drop_index("ix_product_intake_items_updated_at", table_name="product_intake_items")
    op.drop_table("product_intake_items")
