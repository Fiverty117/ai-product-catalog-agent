"""Add mutually exclusive Product or SKU Photo ownership.

Revision ID: 20260915_0008
Revises: 20260914_0007
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260915_0008"
down_revision: str | None = "20260914_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    with op.batch_alter_table("photos") as batch_op:
        batch_op.add_column(
            sa.Column("product_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_photos_product_id_products",
            "products",
            ["product_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_photos_single_owner",
            "product_id IS NULL OR sku_id IS NULL",
        )
        batch_op.create_index("ix_photos_product_id", ["product_id"])

    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError("foreign-key violations found after Photo ownership migration")
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    with op.batch_alter_table("photos") as batch_op:
        batch_op.drop_index("ix_photos_product_id")
        batch_op.drop_constraint("ck_photos_single_owner", type_="check")
        batch_op.drop_constraint(
            "fk_photos_product_id_products",
            type_="foreignkey",
        )
        batch_op.drop_column("product_id")

    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError("foreign-key violations found after Photo ownership downgrade")
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
