"""Repair the five-position accent-color CHECK from applied 0015 databases.

Revision ID: 20260923_0016
Revises: 20260922_0015
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260923_0016"
down_revision: str | None = "20260922_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCENT_CHECK_NAME = "ck_catalog_brand_profiles_accent_color"
HEX_POSITION = "[0-9A-F]"
CORRECT_ACCENT_CHECK = (
    "length(accent_color) = 7 AND accent_color GLOB '#"
    + HEX_POSITION * 6
    + "'"
)


def upgrade() -> None:
    bind = op.get_bind()
    checks = {
        check["name"]: check["sqltext"]
        for check in sa.inspect(bind).get_check_constraints("catalog_brand_profiles")
    }
    existing = checks.get(ACCENT_CHECK_NAME)
    if existing is None:
        raise RuntimeError("catalog brand accent-color CHECK is missing")
    positions = existing.count(HEX_POSITION)
    if positions == 6 and "length(accent_color) = 7" in existing:
        # A clean database ran the corrected 0015 source; no table copy needed.
        return
    if positions != 5 or "length(accent_color) = 7" not in existing:
        raise RuntimeError("catalog brand accent-color CHECK is not the known 0015 defect")

    # SQLite cannot ALTER a CHECK. Recreate only this table with Alembic batch,
    # preserving its reflected columns, PK, other checks, unique key, FK and index.
    # The temporary FK toggle permits the table swap while render runs may refer
    # to its name. Restore the original FK setting and verify all references.
    with op.get_context().autocommit_block():
        original_fk = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("catalog_brand_profiles", recreate="always") as batch:
                batch.drop_constraint(ACCENT_CHECK_NAME, type_="check")
                batch.create_check_constraint(ACCENT_CHECK_NAME, CORRECT_ACCENT_CHECK)
        finally:
            bind.exec_driver_sql(f"PRAGMA foreign_keys={original_fk}")
        if bind.exec_driver_sql("PRAGMA foreign_key_check").all():
            raise RuntimeError("catalog brand accent-color repair left invalid foreign keys")


def downgrade() -> None:
    # 0015 source has also been corrected. Do not reintroduce a known-invalid CHECK.
    pass
