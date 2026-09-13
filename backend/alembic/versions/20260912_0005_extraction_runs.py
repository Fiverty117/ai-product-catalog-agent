"""Add extraction runs and photo associations.

Revision ID: 20260912_0005
Revises: 20260910_0004
Create Date: 2026-09-12
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260912_0005"
down_revision: str | None = "20260910_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("sku_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="extraction_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("structured_result", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_extraction_runs_provider_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_extraction_runs_model_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_extraction_runs_prompt_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_extraction_runs_schema_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(parameters_hash) = 64 "
            "AND parameters_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_extraction_runs_parameters_hash_format",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extraction_runs_job_id", "extraction_runs", ["job_id"])
    op.create_index("ix_extraction_runs_sku_id", "extraction_runs", ["sku_id"])
    op.create_table(
        "extraction_run_photos",
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"]),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"]),
        sa.PrimaryKeyConstraint("extraction_run_id", "photo_id"),
    )
    op.create_index(
        "ix_extraction_run_photos_photo_id",
        "extraction_run_photos",
        ["photo_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_run_photos_photo_id",
        table_name="extraction_run_photos",
    )
    op.drop_table("extraction_run_photos")
    op.drop_index("ix_extraction_runs_sku_id", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_job_id", table_name="extraction_runs")
    op.drop_table("extraction_runs")
