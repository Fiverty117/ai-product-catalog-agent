"""Add catalog render attempts and immutable PDF artifacts.

Revision ID: 20260921_0014
Revises: 20260920_0013
Create Date: 2026-09-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260921_0014"
down_revision: str | None = "20260920_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_render_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("template_key", sa.String(length=100), nullable=False),
        sa.Column("template_version", sa.String(length=100), nullable=False),
        sa.Column("template_hash", sa.String(length=64), nullable=False),
        sa.Column("renderer_version", sa.String(length=100), nullable=False),
        sa.Column("renderer_engine", sa.String(length=100), nullable=False),
        sa.Column("renderer_engine_version", sa.String(length=255), nullable=True),
        sa.Column("locale", sa.String(length=35), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="catalog_render_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(template_key)) > 0",
            name="ck_catalog_render_runs_template_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(template_version)) > 0",
            name="ck_catalog_render_runs_template_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(template_hash) = 64 "
            "AND template_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_render_runs_template_hash_format",
        ),
        sa.CheckConstraint(
            "length(trim(renderer_version)) > 0",
            name="ck_catalog_render_runs_renderer_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(renderer_engine)) > 0",
            name="ck_catalog_render_runs_renderer_engine_nonempty",
        ),
        sa.CheckConstraint(
            "renderer_engine_version IS NULL "
            "OR length(trim(renderer_engine_version)) > 0",
            name="ck_catalog_render_runs_engine_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(locale)) > 0",
            name="ck_catalog_render_runs_locale_nonempty",
        ),
        sa.CheckConstraint(
            "length(config_hash) = 64 "
            "AND config_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_render_runs_config_hash_format",
        ),
        sa.ForeignKeyConstraint(["catalog_snapshot_id"], ["catalog_snapshots.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_render_runs_catalog_snapshot_id",
        "catalog_render_runs",
        ["catalog_snapshot_id"],
    )
    op.create_index(
        "ix_catalog_render_runs_job_id", "catalog_render_runs", ["job_id"]
    )
    op.create_table(
        "catalog_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("render_run_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "media_type = 'application/pdf'",
            name="ck_catalog_artifacts_pdf_media_type",
        ),
        sa.CheckConstraint(
            "length(trim(file_path)) > 0",
            name="ck_catalog_artifacts_file_path_nonempty",
        ),
        sa.CheckConstraint(
            "length(checksum_sha256) = 64 "
            "AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_artifacts_checksum_sha256_format",
        ),
        sa.CheckConstraint(
            "file_size_bytes > 0",
            name="ck_catalog_artifacts_file_size_positive",
        ),
        sa.CheckConstraint(
            "page_count > 0",
            name="ck_catalog_artifacts_page_count_positive",
        ),
        sa.ForeignKeyConstraint(["catalog_snapshot_id"], ["catalog_snapshots.id"]),
        sa.ForeignKeyConstraint(["render_run_id"], ["catalog_render_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "render_run_id", name="uq_catalog_artifacts_render_run"
        ),
    )
    op.create_index(
        "ix_catalog_artifacts_catalog_snapshot_id",
        "catalog_artifacts",
        ["catalog_snapshot_id"],
    )
    op.create_index(
        "ix_catalog_artifacts_checksum_sha256",
        "catalog_artifacts",
        ["checksum_sha256"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catalog_artifacts_checksum_sha256", table_name="catalog_artifacts"
    )
    op.drop_index(
        "ix_catalog_artifacts_catalog_snapshot_id", table_name="catalog_artifacts"
    )
    op.drop_table("catalog_artifacts")
    op.drop_index("ix_catalog_render_runs_job_id", table_name="catalog_render_runs")
    op.drop_index(
        "ix_catalog_render_runs_catalog_snapshot_id",
        table_name="catalog_render_runs",
    )
    op.drop_table("catalog_render_runs")
