"""Add immutable Product copy runs and human reviews.

Revision ID: 20260925_0018
Revises: 20260924_0017
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260925_0018"
down_revision: str | None = "20260924_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_copy_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("copy_type", sa.String(length=17), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("generated_text", sa.Text(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "copy_type IN ('short_description')",
            name="product_copy_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="product_copy_run_status",
        ),
        sa.CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_product_copy_runs_provider_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_product_copy_runs_model_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_product_copy_runs_prompt_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_product_copy_runs_schema_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(source_fingerprint) = 64 "
            "AND source_fingerprint NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_product_copy_runs_source_fingerprint_format",
        ),
        sa.CheckConstraint(
            "generated_text IS NULL OR "
            "(length(trim(generated_text)) > 0 AND length(generated_text) <= 180)",
            name="ck_product_copy_runs_generated_text_length",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_product_copy_runs_job_id", "product_copy_runs", ["job_id"]
    )
    op.create_index(
        "ix_product_copy_runs_product_id", "product_copy_runs", ["product_id"]
    )
    op.create_index(
        "ix_product_copy_runs_source_fingerprint",
        "product_copy_runs",
        ["source_fingerprint"],
    )
    op.create_table(
        "product_copy_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_copy_run_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=9), nullable=False),
        sa.Column("corrected_short_description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "decision IN ('approved', 'corrected', 'rejected')",
            name="product_copy_review_decision",
        ),
        sa.CheckConstraint(
            "(decision = 'corrected' AND corrected_short_description IS NOT NULL) OR "
            "(decision IN ('approved', 'rejected') "
            "AND corrected_short_description IS NULL)",
            name="ck_product_copy_reviews_corrected_text",
        ),
        sa.CheckConstraint(
            "corrected_short_description IS NULL OR "
            "(length(trim(corrected_short_description)) > 0 "
            "AND length(corrected_short_description) <= 180)",
            name="ck_product_copy_reviews_corrected_text_length",
        ),
        sa.CheckConstraint(
            "(decision = 'rejected' AND applied_at IS NULL) OR "
            "(decision IN ('approved', 'corrected') AND applied_at IS NOT NULL)",
            name="ck_product_copy_reviews_applied_at",
        ),
        sa.ForeignKeyConstraint(
            ["product_copy_run_id"], ["product_copy_runs.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_copy_run_id", name="uq_product_copy_reviews_run"
        ),
    )


def downgrade() -> None:
    op.drop_table("product_copy_reviews")
    op.drop_index(
        "ix_product_copy_runs_source_fingerprint", table_name="product_copy_runs"
    )
    op.drop_index("ix_product_copy_runs_product_id", table_name="product_copy_runs")
    op.drop_index("ix_product_copy_runs_job_id", table_name="product_copy_runs")
    op.drop_table("product_copy_runs")
