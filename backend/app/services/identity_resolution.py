import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Brand,
    ExtractionIdentityResolution,
    ExtractionRun,
    Product,
    SKU,
)
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus, IdentityResolutionAction
from app.domain.identity import identity_key_v1, loose_candidate_key
from app.domain.schemas import (
    CreateNewBrandDecision,
    CreateNewProductDecision,
    ExtractionIdentityResolutionRequest,
    UseExistingBrandDecision,
    UseExistingProductDecision,
)


class IdentityResolutionError(ValueError):
    pass


class UnknownIdentityExtractionRunError(IdentityResolutionError):
    pass


class UnreviewableIdentityExtractionRunError(IdentityResolutionError):
    pass


class DuplicateIdentityResolutionError(IdentityResolutionError):
    pass


class UnknownBrandError(IdentityResolutionError):
    pass


class DuplicateBrandIdentityError(IdentityResolutionError):
    pass


class UnknownProductError(IdentityResolutionError):
    pass


class DuplicateProductIdentityError(IdentityResolutionError):
    pass


class ProductBrandMismatchError(IdentityResolutionError):
    pass


class SKUProductConflictError(IdentityResolutionError):
    pass


def find_brand_identity_candidates(session: Session, name: str) -> list[Brand]:
    """Return deterministic exact/loose suggestions without selecting one."""

    exact_key = identity_key_v1(name)
    loose_key = loose_candidate_key(name)
    brands = session.scalars(select(Brand).order_by(Brand.name, Brand.id)).all()
    return [
        brand
        for brand in brands
        if brand.identity_key == exact_key
        or loose_candidate_key(brand.name) == loose_key
    ]


def find_product_identity_candidates(
    session: Session,
    *,
    brand_id: uuid.UUID,
    name: str,
) -> list[Product]:
    """Return deterministic suggestions scoped to one Brand."""

    exact_key = identity_key_v1(name)
    loose_key = loose_candidate_key(name)
    products = session.scalars(
        select(Product)
        .where(Product.brand_id == brand_id)
        .order_by(Product.name, Product.id)
    ).all()
    return [
        product
        for product in products
        if product.identity_key == exact_key
        or loose_candidate_key(product.name) == loose_key
    ]


def resolve_extraction_identity(
    session: Session,
    request: ExtractionIdentityResolutionRequest | Mapping[str, Any],
    *,
    applied_at: datetime | None = None,
) -> ExtractionIdentityResolution:
    """Resolve canonical Brand/Product identity in one caller-owned transaction."""

    validated = ExtractionIdentityResolutionRequest.model_validate(request)
    run = session.get(ExtractionRun, validated.extraction_run_id)
    if run is None:
        raise UnknownIdentityExtractionRunError(
            f"extraction run not found: {validated.extraction_run_id}"
        )
    if run.status is not ExtractionRunStatus.SUCCEEDED:
        raise UnreviewableIdentityExtractionRunError(
            f"extraction run must be succeeded: {run.id}"
        )
    existing_resolution = session.scalar(
        select(ExtractionIdentityResolution).where(
            ExtractionIdentityResolution.extraction_run_id == run.id
        )
    )
    if existing_resolution is not None:
        raise DuplicateIdentityResolutionError(
            f"identity resolution already exists for extraction run: {run.id}"
        )

    brand = _resolve_brand(session, validated.brand)
    product = _resolve_product(session, brand, validated.product)
    if product.brand_id != brand.id:
        raise ProductBrandMismatchError(
            "resolved Product does not belong to the resolved Brand"
        )
    _require_sku_product_match(session, run, product)

    resolution = ExtractionIdentityResolution(
        extraction_run=run,
        brand=brand,
        product=product,
        brand_action=IdentityResolutionAction(validated.brand.action),
        product_action=IdentityResolutionAction(validated.product.action),
        applied_at=applied_at or utc_now(),
    )
    session.add(resolution)
    session.flush()
    return resolution


def _resolve_brand(
    session: Session,
    decision: UseExistingBrandDecision | CreateNewBrandDecision,
) -> Brand:
    if isinstance(decision, UseExistingBrandDecision):
        brand = session.get(Brand, decision.brand_id)
        if brand is None:
            raise UnknownBrandError(f"Brand not found: {decision.brand_id}")
        return brand

    identity_key = identity_key_v1(decision.name)
    existing = session.scalar(
        select(Brand).where(Brand.identity_key == identity_key)
    )
    if existing is not None:
        raise DuplicateBrandIdentityError(
            f"Brand already exists; use_existing with brand_id={existing.id}"
        )
    brand = Brand(name=decision.name)
    session.add(brand)
    session.flush()
    return brand


def _resolve_product(
    session: Session,
    brand: Brand,
    decision: UseExistingProductDecision | CreateNewProductDecision,
) -> Product:
    if isinstance(decision, UseExistingProductDecision):
        product = session.get(Product, decision.product_id)
        if product is None:
            raise UnknownProductError(f"Product not found: {decision.product_id}")
        if product.brand_id != brand.id:
            raise ProductBrandMismatchError(
                f"Product {product.id} does not belong to Brand {brand.id}"
            )
        return product

    identity_key = identity_key_v1(decision.name)
    existing = session.scalar(
        select(Product).where(
            Product.brand_id == brand.id,
            Product.identity_key == identity_key,
        )
    )
    if existing is not None:
        raise DuplicateProductIdentityError(
            f"Product already exists under this Brand; use_existing with "
            f"product_id={existing.id}"
        )
    product = Product(brand=brand, name=decision.name)
    session.add(product)
    session.flush()
    return product


def _require_sku_product_match(
    session: Session,
    run: ExtractionRun,
    product: Product,
) -> None:
    if run.sku_id is None:
        return
    sku = session.get(SKU, run.sku_id)
    if sku is None:
        raise SKUProductConflictError(
            f"extraction run references missing SKU: {run.sku_id}"
        )
    if sku.product_id != product.id:
        raise SKUProductConflictError(
            f"resolved Product {product.id} conflicts with SKU {sku.id} Product "
            f"{sku.product_id}; SKU reassignment is not allowed"
        )
