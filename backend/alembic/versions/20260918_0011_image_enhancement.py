"""Add image enhancement attempts and immutable derived assets.

Revision ID: 20260918_0011
Revises: 20260917_0010
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260918_0011"
down_revision: str | None = "20260917_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_enhancement_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_photo_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("config_version", sa.String(length=100), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="image_enhancement_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_image_enhancement_runs_provider_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_image_enhancement_runs_model_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_image_enhancement_runs_prompt_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(config_version)) > 0",
            name="ck_image_enhancement_runs_config_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(parameters_hash) = 64 "
            "AND parameters_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_image_enhancement_runs_parameters_hash_format",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["source_photo_id"], ["photos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_image_enhancement_runs_source_photo_id",
        "image_enhancement_runs",
        ["source_photo_id"],
    )
    op.create_index(
        "ix_image_enhancement_runs_job_id",
        "image_enhancement_runs",
        ["job_id"],
    )
    op.create_table(
        "derived_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_photo_id", sa.Uuid(), nullable=False),
        sa.Column("enhancement_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=32), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(file_path)) > 0",
            name="ck_derived_images_file_path_nonempty",
        ),
        sa.CheckConstraint(
            "length(checksum_sha256) = 64 "
            "AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_derived_images_checksum_sha256_format",
        ),
        sa.CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_derived_images_supported_mime_type",
        ),
        sa.CheckConstraint(
            "file_size_bytes > 0",
            name="ck_derived_images_file_size_positive",
        ),
        sa.CheckConstraint("width > 0", name="ck_derived_images_width_positive"),
        sa.CheckConstraint("height > 0", name="ck_derived_images_height_positive"),
        sa.ForeignKeyConstraint(
            ["enhancement_run_id"], ["image_enhancement_runs.id"]
        ),
        sa.ForeignKeyConstraint(["source_photo_id"], ["photos.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "enhancement_run_id", name="uq_derived_images_enhancement_run"
        ),
    )
    op.create_index(
        "ix_derived_images_source_photo_id",
        "derived_images",
        ["source_photo_id"],
    )
    op.create_index(
        "ix_derived_images_checksum_sha256",
        "derived_images",
        ["checksum_sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_derived_images_checksum_sha256", table_name="derived_images")
    op.drop_index("ix_derived_images_source_photo_id", table_name="derived_images")
    op.drop_table("derived_images")
    op.drop_index(
        "ix_image_enhancement_runs_job_id", table_name="image_enhancement_runs"
    )
    op.drop_index(
        "ix_image_enhancement_runs_source_photo_id",
        table_name="image_enhancement_runs",
    )
    op.drop_table("image_enhancement_runs")
