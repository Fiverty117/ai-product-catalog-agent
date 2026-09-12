"""Add durable job queue.

Revision ID: 20260910_0004
Revises: 20260909_0003
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260910_0004"
down_revision: str | None = "20260909_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="job_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(trim(job_type)) > 0",
            name="ck_jobs_job_type_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_jobs_idempotency_key_nonempty",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts_nonnegative"),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_jobs_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name="ck_jobs_attempts_within_maximum",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        ["status", "next_retry_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
