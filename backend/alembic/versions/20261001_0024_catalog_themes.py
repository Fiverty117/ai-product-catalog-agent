"""Audit frozen catalog theme on v3 render attempts.

Revision ID: 20261001_0024
Revises: 20260930_0023
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261001_0024"
down_revision: str | None = "20260930_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("catalog_render_runs") as batch:
        batch.add_column(sa.Column("theme_schema_version", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("theme_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("theme_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("catalog_render_runs") as batch:
        batch.drop_column("theme_data")
        batch.drop_column("theme_hash")
        batch.drop_column("theme_schema_version")
