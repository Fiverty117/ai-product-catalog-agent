"""One atomic, explicitly human-confirmed Intake-to-canonical promotion."""

import hashlib
import json
import uuid
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Brand, Category, Product, ProductIntakeItem, ProductIntakePromotion, SKU
from app.db.types import utc_now
from app.domain.enums import PhotoRole
from app.domain.identity import clean_identity_display_name, identity_key_v1
from app.domain.product_intake import (
    ProductIntakeDraft, ProductIntakePromotionContext, ProductIntakePromotionRequest,
    ProductIntakePromotionResult, PromotionCategoryOption,
)
from app.domain.schemas import ProductPriceEditRequest, SKUCreate
from app.services.catalog_builder import get_catalog_builder_product_summary
from app.services.categories import assign_product_category, list_active_categories
from app.services.identity_resolution import find_brand_identity_candidates, find_product_identity_candidates
from app.services.photo_ownership import assign_photo_to_product
from app.services.product_data_editorial import change_product_price
from app.services.product_intake import intake_read, require_intake, resolve_intake_photo
from app.services.sku_variants import create_manual_sku, find_sku_variant_candidates, DuplicateSKUVariantError


class PromotionConflictError(ValueError):
    pass


class PromotionInputError(ValueError):
    pass


class PromotionResourceError(ValueError):
    pass


def _request_hash(request: ProductIntakePromotionRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _promotion_result(session: Session, promotion: ProductIntakePromotion) -> ProductIntakePromotionResult:
    product = session.get(Product, promotion.product_id)
    if product is None:
        raise PromotionResourceError("Promoted Product is missing.")
    skus = session.scalars(
        select(SKU).where(SKU.product_id == product.id).order_by(SKU.created_at, SKU.id)
    ).all()
    return ProductIntakePromotionResult(
        intake_id=promotion.intake_item_id, product_id=product.id,
        brand_id=product.brand_id, brand_name=product.brand.name,
        brand_reused=promotion.brand_reused, promoted_at=promotion.promoted_at,
        sku_ids=[sku.id for sku in skus],
        product=get_catalog_builder_product_summary(session, product_id=product.id),
    )


def promotion_context(session: Session, intake_id: uuid.UUID) -> ProductIntakePromotionContext:
    item = require_intake(session, intake_id)
    categories = list_active_categories(session)
    return ProductIntakePromotionContext(
        categories=[PromotionCategoryOption(id=category.id, name=category.name) for category in categories],
        result=_promotion_result(session, item.promotion) if item.promotion else None,
    )


def promote_intake(
    session: Session,
    intake_id: uuid.UUID,
    request: ProductIntakePromotionRequest,
    *,
    originals_dir: Path,
) -> ProductIntakePromotionResult:
    """Acquire SQLite's writer lock, validate saved state, then flush one graph.

    The caller commits once after this returns or rolls back on any exception.
    """
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    item = require_intake(session, intake_id)
    request_hash = _request_hash(request)
    if item.promotion is not None:
        if item.promotion.idempotency_key == request.idempotency_key and item.promotion.request_hash == request_hash:
            return _promotion_result(session, item.promotion)
        raise PromotionConflictError("This Intake Item was already promoted to a canonical Product.")
    other = session.scalar(select(ProductIntakePromotion).where(ProductIntakePromotion.idempotency_key == request.idempotency_key))
    if other is not None:
        raise PromotionConflictError("Idempotency key belongs to another promotion.")
    intake_read(session, item)  # Reconcile durable extraction state before lifecycle validation.
    if item.status in {"queued", "running"}:
        raise PromotionConflictError("Extraction is active; wait before promoting.")
    if not item.photos or not any(link.is_primary for link in item.photos):
        raise PromotionInputError("At least one source photo and a primary photo are required.")

    try:
        draft = ProductIntakeDraft.model_validate(item.draft)
    except ValidationError as exc:
        raise PromotionInputError("Saved Intake Draft is invalid.") from exc
    if not draft.brand_name or not draft.product_name:
        raise PromotionInputError("Saved Intake Draft requires Brand and Product name.")
    if not draft.skus:
        raise PromotionInputError("Add at least one reviewed variant before promotion.")
    if any(not any((sku.flavor, sku.size_value, sku.servings, sku.external_sku)) for sku in draft.skus):
        raise PromotionInputError("Each variant needs at least one identifying field.")
    if any(sku.size_unit is not None and len(sku.size_unit) > 32 for sku in draft.skus):
        raise PromotionInputError("Variant size unit must be at most 32 characters.")
    if any(price.intake_sku_index >= len(draft.skus) for price in request.sku_prices):
        raise PromotionInputError("Initial Price references an unknown variant.")
    for price in request.sku_prices:
        ProductPriceEditRequest(amount=price.amount, currency=price.currency)

    selected_ids = ([request.primary_category_id] if request.primary_category_id else []) + request.secondary_category_ids
    for category_id in selected_ids:
        category = session.get(Category, category_id)
        if category is None:
            raise PromotionResourceError("Selected Category was not found.")
        if not category.is_active:
            raise PromotionInputError("Selected Category is inactive.")

    # Verify every immutable source asset before any canonical ownership transition.
    for link in item.photos:
        resolve_intake_photo(session, item, link.photo_id, originals_dir)

    brand_name = clean_identity_display_name(draft.brand_name)
    product_name = clean_identity_display_name(draft.product_name)
    if len(brand_name) > 255 or len(product_name) > 255:
        raise PromotionInputError("Brand and Product names must be at most 255 characters.")
    brand_key = identity_key_v1(brand_name)
    brand = session.scalar(select(Brand).where(Brand.identity_key == brand_key))
    brand_reused = brand is not None
    if brand is None:
        candidates = find_brand_identity_candidates(session, brand_name)
        if candidates:
            raise PromotionConflictError(f"A similar canonical Brand already exists: {candidates[0].name}.")
        brand = Brand(name=brand_name)
        session.add(brand)
        session.flush()

    product_key = identity_key_v1(product_name)
    existing = session.scalar(select(Product).where(Product.brand_id == brand.id, Product.identity_key == product_key))
    if existing is not None:
        raise PromotionConflictError(f"A canonical Product with this identity already exists: {brand.name} / {existing.name}.")
    candidates = find_product_identity_candidates(session, brand_id=brand.id, name=product_name)
    if candidates:
        raise PromotionConflictError(f"A similar canonical Product already exists: {brand.name} / {candidates[0].name}.")
    product = Product(brand=brand, name=product_name)
    session.add(product)
    session.flush()

    skus: list[SKU] = []
    for candidate in draft.skus:
        sku_request = SKUCreate(
            product_id=product.id, flavor=candidate.flavor,
            size_value=candidate.size_value, size_unit=candidate.size_unit,
            servings=candidate.servings, external_sku=candidate.external_sku,
        )
        matches = find_sku_variant_candidates(session, sku_request)
        if matches.external_sku:
            raise PromotionConflictError("Duplicate external SKU in reviewed variants.")
        try:
            skus.append(create_manual_sku(session, sku_request))
        except DuplicateSKUVariantError as exc:
            raise PromotionConflictError("Duplicate canonical SKU variant in reviewed draft.") from exc

    if request.primary_category_id is not None:
        assign_product_category(session, product_id=product.id, category_id=request.primary_category_id, is_primary=True)
    for category_id in request.secondary_category_ids:
        assign_product_category(session, product_id=product.id, category_id=category_id)

    for link in sorted(item.photos, key=lambda row: row.position):
        photo = assign_photo_to_product(session, photo_id=link.photo_id, product_id=product.id)
        if link.is_primary:
            photo.role = PhotoRole.FRONT
        elif photo.role is PhotoRole.FRONT:
            photo.role = PhotoRole.OTHER
    session.flush()

    for price in request.sku_prices:
        change_product_price(
            session, product.id, skus[price.intake_sku_index].id,
            ProductPriceEditRequest(amount=price.amount, currency=price.currency),
        )

    promotion = ProductIntakePromotion(
        item=item, product=product, idempotency_key=request.idempotency_key,
        request_hash=request_hash, brand_reused=brand_reused, promoted_at=utc_now(),
    )
    session.add(promotion)
    item.status = "promoted"
    item.updated_at = utc_now()
    session.flush()
    return _promotion_result(session, promotion)
