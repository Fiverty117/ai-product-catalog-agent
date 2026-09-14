import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Product, SKU
from app.domain.enums import FieldSource, SKUFieldName
from app.domain.schemas import SKUCreate
from app.domain.sku_variants import (
    SKUVariantCandidates,
    is_partial_variant_match,
    sku_variant_tuple,
    variant_tuple,
)
from app.services.sku_field_provenance import apply_sku_field_update


class SKUVariantError(ValueError):
    pass


class UnknownSKUProductError(SKUVariantError):
    pass


class DuplicateSKUVariantError(SKUVariantError):
    def __init__(self, existing_sku: SKU):
        self.existing_sku = existing_sku
        super().__init__(
            "exact SKU variant already exists under this Product; use existing "
            f"SKU {existing_sku.id}"
        )


def find_sku_variant_candidates(
    session: Session,
    request: SKUCreate | Mapping[str, Any],
) -> SKUVariantCandidates:
    """Return scoped deterministic candidates without selecting or merging one."""

    validated = SKUCreate.model_validate(request)
    _require_product(session, validated.product_id)
    target = variant_tuple(
        flavor=validated.flavor,
        size_value=validated.size_value,
        size_unit=validated.size_unit,
        servings=validated.servings,
    )
    skus = session.scalars(
        select(SKU)
        .where(SKU.product_id == validated.product_id)
        .order_by(SKU.created_at, SKU.id)
    ).all()

    exact: list[SKU] = []
    partial: list[SKU] = []
    external: list[SKU] = []
    for sku in skus:
        stored = sku_variant_tuple(sku)
        if stored == target:
            exact.append(sku)
        elif is_partial_variant_match(stored, target):
            partial.append(sku)
        if (
            validated.external_sku is not None
            and sku.external_sku == validated.external_sku
        ):
            external.append(sku)

    return SKUVariantCandidates(
        exact=tuple(exact),
        partial=tuple(partial),
        external_sku=tuple(external),
    )


def create_manual_sku(
    session: Session,
    request: SKUCreate | Mapping[str, Any],
) -> SKU:
    """Create one explicit human SKU and locked provenance without committing."""

    validated = SKUCreate.model_validate(request)
    product = _require_product(session, validated.product_id)
    candidates = find_sku_variant_candidates(session, validated)
    if candidates.exact:
        raise DuplicateSKUVariantError(candidates.exact[0])

    sku = SKU(product=product)
    session.add(sku)
    session.flush()

    supplied = (
        (SKUFieldName.EXTERNAL_SKU, validated.external_sku),
        (SKUFieldName.FLAVOR, validated.flavor),
        (SKUFieldName.SIZE_VALUE, validated.size_value),
        (SKUFieldName.SIZE_UNIT, validated.size_unit),
        (SKUFieldName.SERVINGS, validated.servings),
    )
    for field_name, value in supplied:
        if value is not None:
            apply_sku_field_update(
                session,
                sku=sku,
                field_name=field_name,
                value=value,
                source=FieldSource.HUMAN,
            )

    session.flush()
    return sku


def _require_product(session: Session, product_id: uuid.UUID) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownSKUProductError(f"Product not found: {product_id}")
    return product
