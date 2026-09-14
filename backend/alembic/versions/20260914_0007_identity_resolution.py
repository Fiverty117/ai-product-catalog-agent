"""Add deterministic Brand/Product identity resolution.

Revision ID: 20260914_0007
Revises: 20260913_0006
Create Date: 2026-09-14
"""

import unicodedata
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260914_0007"
down_revision: str | None = "20260913_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    brands = connection.execute(
        sa.text("SELECT id, name FROM brands ORDER BY id")
    ).mappings().all()
    products = connection.execute(
        sa.text("SELECT id, brand_id, name FROM products ORDER BY id")
    ).mappings().all()

    prepared_brands = [
        (row["id"], *_normalized_name_and_key(row["name"])) for row in brands
    ]
    prepared_products = [
        (row["id"], row["brand_id"], *_normalized_name_and_key(row["name"]))
        for row in products
    ]
    _reject_brand_collisions(prepared_brands)
    _reject_product_collisions(prepared_products)

    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    op.add_column(
        "brands",
        sa.Column("identity_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("identity_key", sa.String(length=512), nullable=True),
    )

    for brand_id, display_name, identity_key in prepared_brands:
        connection.execute(
            sa.text(
                "UPDATE brands SET name = :name, identity_key = :identity_key "
                "WHERE id = :id"
            ),
            {"id": brand_id, "name": display_name, "identity_key": identity_key},
        )
    for product_id, _brand_id, display_name, identity_key in prepared_products:
        connection.execute(
            sa.text(
                "UPDATE products SET name = :name, identity_key = :identity_key "
                "WHERE id = :id"
            ),
            {"id": product_id, "name": display_name, "identity_key": identity_key},
        )

    with op.batch_alter_table("brands") as batch_op:
        batch_op.alter_column(
            "identity_key",
            existing_type=sa.String(length=512),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_brands_identity_key_nonempty",
            "length(identity_key) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_brands_identity_key",
            ["identity_key"],
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "identity_key",
            existing_type=sa.String(length=512),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_products_identity_key_nonempty",
            "length(identity_key) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_products_brand_identity_key",
            ["brand_id", "identity_key"],
        )

    op.create_table(
        "extraction_identity_resolutions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column(
            "brand_action",
            sa.Enum(
                "use_existing",
                "create_new",
                name="brand_identity_resolution_action",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "product_action",
            sa.Enum(
                "use_existing",
                "create_new",
                name="product_identity_resolution_action",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"]),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_run_id",
            name="uq_extraction_identity_resolutions_run",
        ),
    )
    op.create_index(
        "ix_extraction_identity_resolutions_brand_id",
        "extraction_identity_resolutions",
        ["brand_id"],
    )
    op.create_index(
        "ix_extraction_identity_resolutions_product_id",
        "extraction_identity_resolutions",
        ["product_id"],
    )
    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError("foreign-key violations found after identity migration")
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    op.drop_index(
        "ix_extraction_identity_resolutions_product_id",
        table_name="extraction_identity_resolutions",
    )
    op.drop_index(
        "ix_extraction_identity_resolutions_brand_id",
        table_name="extraction_identity_resolutions",
    )
    op.drop_table("extraction_identity_resolutions")

    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_constraint(
            "uq_products_brand_identity_key",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_products_identity_key_nonempty",
            type_="check",
        )
        batch_op.drop_column("identity_key")

    with op.batch_alter_table("brands") as batch_op:
        batch_op.drop_constraint("uq_brands_identity_key", type_="unique")
        batch_op.drop_constraint(
            "ck_brands_identity_key_nonempty",
            type_="check",
        )
        batch_op.drop_column("identity_key")

    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError("foreign-key violations found after identity downgrade")
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _normalized_name_and_key(name: str) -> tuple[str, str]:
    display_name = " ".join(name.split())
    normalized = unicodedata.normalize("NFKC", name)
    identity_key = " ".join(normalized.split()).casefold()
    if not identity_key:
        raise RuntimeError("Identity-key v1 cannot normalize an empty name")
    return display_name, identity_key


def _reject_brand_collisions(
    prepared: list[tuple[str, str, str]],
) -> None:
    seen: dict[str, tuple[str, str]] = {}
    for brand_id, display_name, identity_key in prepared:
        previous = seen.get(identity_key)
        if previous is not None:
            raise RuntimeError(
                "Identity-key v1 Brand collision; manually rename or remove one "
                f"record before retrying migration: {previous[0]} "
                f"({previous[1]!r}) and {brand_id} ({display_name!r})"
            )
        seen[identity_key] = (brand_id, display_name)


def _reject_product_collisions(
    prepared: list[tuple[str, str, str, str]],
) -> None:
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for product_id, brand_id, display_name, identity_key in prepared:
        scoped_key = (brand_id, identity_key)
        previous = seen.get(scoped_key)
        if previous is not None:
            raise RuntimeError(
                "Identity-key v1 Product collision within one Brand; manually "
                "rename or remove one record before retrying migration: "
                f"{previous[0]} ({previous[1]!r}) and {product_id} "
                f"({display_name!r})"
            )
        seen[scoped_key] = (product_id, display_name)
