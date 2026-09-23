"""Add narrow idempotent Catalog Builder orchestration.

Revision ID: 20260928_0021
Revises: 20260927_0020
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260928_0021"
down_revision: str | None = "20260927_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_builds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_catalog_builds_idempotency_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_builds_request_hash_format",
        ),
        sa.ForeignKeyConstraint(["catalog_snapshot_id"], ["catalog_snapshots.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_catalog_builds_idempotency_key"),
        sa.UniqueConstraint("catalog_snapshot_id", name="uq_catalog_builds_snapshot"),
        sa.UniqueConstraint("job_id", name="uq_catalog_builds_job"),
    )
    op.create_index("ix_catalog_builds_created_at", "catalog_builds", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_catalog_builds_created_at", table_name="catalog_builds")
    op.drop_table("catalog_builds")
