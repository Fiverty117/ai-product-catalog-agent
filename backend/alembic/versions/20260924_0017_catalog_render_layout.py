"""Add explicit catalog render layout audit fields.

Revision ID: 20260924_0017
Revises: 20260923_0016
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260924_0017"
down_revision: str | None = "20260923_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "catalog_render_runs",
        sa.Column("layout_key", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "catalog_render_runs",
        sa.Column("layout_version", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("catalog_render_runs", "layout_version")
    op.drop_column("catalog_render_runs", "layout_key")
