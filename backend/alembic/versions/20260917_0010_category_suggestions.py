"""Add category suggestion runs, reviews and canonical lineage.

Revision ID: 20260917_0010
Revises: 20260916_0009
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260917_0010"
down_revision: str | None = "20260916_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "category_suggestion_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="category_suggestion_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("structured_result", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_category_suggestion_runs_provider_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_category_suggestion_runs_model_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_category_suggestion_runs_prompt_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_category_suggestion_runs_schema_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64 "
            "AND input_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_category_suggestion_runs_input_hash_format",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_category_suggestion_runs_product_id",
        "category_suggestion_runs",
        ["product_id"],
    )
    op.create_index(
        "ix_category_suggestion_runs_job_id",
        "category_suggestion_runs",
        ["job_id"],
    )
    op.create_index(
        "ix_category_suggestion_runs_input_hash",
        "category_suggestion_runs",
        ["input_hash"],
    )
    op.create_table(
        "category_suggestion_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("category_suggestion_run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum(
                "accepted",
                "corrected",
                "rejected",
                name="category_suggestion_review_decision",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("final_selection", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(decision = 'corrected' AND final_selection IS NOT NULL) OR "
            "(decision IN ('accepted', 'rejected') AND final_selection IS NULL)",
            name="ck_category_suggestion_reviews_final_selection",
        ),
        sa.CheckConstraint(
            "(decision = 'rejected' AND applied_at IS NULL) OR "
            "(decision IN ('accepted', 'corrected') AND applied_at IS NOT NULL)",
            name="ck_category_suggestion_reviews_applied_at",
        ),
        sa.ForeignKeyConstraint(
            ["category_suggestion_run_id"],
            ["category_suggestion_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "category_suggestion_run_id",
            name="uq_category_suggestion_reviews_run",
        ),
    )

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("product_categories") as batch_op:
        batch_op.add_column(
            sa.Column("category_suggestion_run_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_product_categories_category_suggestion_run_id",
            "category_suggestion_runs",
            ["category_suggestion_run_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_product_categories_category_suggestion_run_id",
            ["category_suggestion_run_id"],
        )
    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError(
                "foreign-key violations found after category suggestion migration"
            )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("product_categories") as batch_op:
        batch_op.drop_index("ix_product_categories_category_suggestion_run_id")
        batch_op.drop_constraint(
            "fk_product_categories_category_suggestion_run_id",
            type_="foreignkey",
        )
        batch_op.drop_column("category_suggestion_run_id")
    op.drop_table("category_suggestion_reviews")
    op.drop_index(
        "ix_category_suggestion_runs_input_hash",
        table_name="category_suggestion_runs",
    )
    op.drop_index(
        "ix_category_suggestion_runs_job_id",
        table_name="category_suggestion_runs",
    )
    op.drop_index(
        "ix_category_suggestion_runs_product_id",
        table_name="category_suggestion_runs",
    )
    op.drop_table("category_suggestion_runs")
    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError(
                "foreign-key violations found after category suggestion downgrade"
            )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
