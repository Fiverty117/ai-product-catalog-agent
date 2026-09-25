"""Record optional provenance for a new Build started from History.

Revision ID: 20261004_0027
Revises: 20261003_0026
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261004_0027"
down_revision: str | None = "20261003_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("catalog_builds") as batch:
        batch.add_column(sa.Column("source_build_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_catalog_builds_source_build_id", "catalog_builds",
            ["source_build_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("catalog_builds") as batch:
        batch.drop_constraint("fk_catalog_builds_source_build_id", type_="foreignkey")
        batch.drop_column("source_build_id")
