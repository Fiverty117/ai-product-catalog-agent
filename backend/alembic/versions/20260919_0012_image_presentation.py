"""Add append-only derived-image reviews and current Photo presentation.

Revision ID: 20260919_0012
Revises: 20260918_0011
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260919_0012"
down_revision: str | None = "20260918_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "derived_image_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("derived_image_id", sa.Uuid(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum(
                "approved",
                "rejected",
                name="derived_image_review_decision",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["derived_image_id"], ["derived_images.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_derived_image_reviews_current",
        "derived_image_reviews",
        ["derived_image_id", "created_at", "id"],
    )
    op.create_table(
        "photo_presentation_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("selected_derived_image_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"]),
        sa.ForeignKeyConstraint(
            ["selected_derived_image_id"], ["derived_images.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "photo_id",
            name="uq_photo_presentation_preferences_photo",
        ),
    )
    op.create_index(
        "ix_photo_presentation_preferences_selected_derived_image_id",
        "photo_presentation_preferences",
        ["selected_derived_image_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_photo_presentation_preferences_selected_derived_image_id",
        table_name="photo_presentation_preferences",
    )
    op.drop_table("photo_presentation_preferences")
    op.drop_index(
        "ix_derived_image_reviews_current",
        table_name="derived_image_reviews",
    )
    op.drop_table("derived_image_reviews")
