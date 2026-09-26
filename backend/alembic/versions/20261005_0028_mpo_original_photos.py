"""Permit true MPO MIME on original Photos only.

Revision ID: 20261005_0028
Revises: 20261004_0027
"""

from alembic import op
import sqlalchemy as sa

revision = "20261005_0028"
down_revision = "20261004_0027"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_photos_supported_mime_type"
ORDINARY_MIME_CHECK = "mime_type IN ('image/jpeg', 'image/png', 'image/webp')"


def _replace_check(expression: str) -> None:
    bind = op.get_bind()
    # Same FK-safe batch convention as 0016; dependent tables retain their rows.
    with op.get_context().autocommit_block():
        original_fk = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("photos", recreate="always") as batch:
                batch.drop_constraint(CHECK_NAME, type_="check")
                batch.create_check_constraint(CHECK_NAME, expression)
        finally:
            bind.exec_driver_sql(f"PRAGMA foreign_keys={original_fk}")
        if bind.exec_driver_sql("PRAGMA foreign_key_check").all():
            raise RuntimeError("Photo MIME migration left invalid foreign keys")


def upgrade() -> None:
    _replace_check(ORDINARY_MIME_CHECK + " OR (is_original = 1 AND mime_type = 'image/mpo')")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM photos WHERE mime_type = 'image/mpo'")):
        raise RuntimeError("Cannot downgrade Photo MIME constraint while MPO Photos exist; no rows were deleted.")
    _replace_check(ORDINARY_MIME_CHECK)
