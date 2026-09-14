import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Price


def select_active_approved_price(
    session: Session,
    *,
    sku_id: uuid.UUID,
    currency: str,
    as_of: datetime,
) -> Price | None:
    """Return the deterministic active approved Price for one SKU context."""

    return session.scalar(
        select(Price)
        .where(
            Price.sku_id == sku_id,
            Price.approved.is_(True),
            Price.currency == currency,
            Price.valid_from <= as_of,
        )
        .order_by(
            Price.valid_from.desc(),
            Price.created_at.desc(),
            Price.id.desc(),
        )
        .limit(1)
    )
