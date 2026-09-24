"""Add nullable v5 closing-page render audit.

Revision ID: 20261003_0026
Revises: 20261002_0025
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261003_0026"
down_revision: str | None = "20261002_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("catalog_render_runs") as batch:
        batch.add_column(sa.Column("closing_schema_version", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("closing_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("closing_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("catalog_render_runs") as batch:
        batch.drop_column("closing_data")
        batch.drop_column("closing_hash")
        batch.drop_column("closing_schema_version")
