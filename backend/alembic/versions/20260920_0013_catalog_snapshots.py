"""Add immutable catalog content snapshots.

Revision ID: 20260920_0013
Revises: 20260919_0012
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260920_0013"
down_revision: str | None = "20260919_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_catalog_snapshots_schema_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency) "
            "AND currency NOT GLOB '*[^A-Z]*'",
            name="ck_catalog_snapshots_currency_iso_code",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64 "
            "AND content_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_snapshots_content_hash_format",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_snapshots_content_hash",
        "catalog_snapshots",
        ["content_hash"],
    )
    op.create_index(
        "ix_catalog_snapshots_created_at",
        "catalog_snapshots",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catalog_snapshots_created_at",
        table_name="catalog_snapshots",
    )
    op.drop_index(
        "ix_catalog_snapshots_content_hash",
        table_name="catalog_snapshots",
    )
    op.drop_table("catalog_snapshots")
