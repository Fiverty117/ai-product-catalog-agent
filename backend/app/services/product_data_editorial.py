"""Explicit human edits to canonical Product commerce facts."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Brand, Category, Price, Product, ProductCategory, ProductIdentityEdit, SKU
from app.db.types import utc_now
from app.domain.enums import FieldSource, SKUFieldName
from app.domain.schemas import (
    ProductCategoryEditRequest, ProductDataBrandSummary, ProductDataCategorySummary,
    ProductDataPriceSummary, ProductDataSKUSummary, ProductDataSummary,
    ProductIdentityEditRequest, ProductPriceEditRequest, ProductSKUEditRequest, SKUCreate,
)
from app.services.catalog_builder import get_catalog_builder_product_summary
from app.services.categories import assign_product_category, list_active_categories, remove_product_category
from app.services.prices import select_active_approved_price
from app.services.sku_field_provenance import apply_sku_field_update
from app.services.sku_variants import create_manual_sku, find_sku_variant_candidates, DuplicateSKUVariantError


class UnknownProductDataError(ValueError):
    pass


class ProductDataConflictError(ValueError):
    pass


def require_product(session: Session, product_id: uuid.UUID) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownProductDataError("Product not found.")
    return product


def require_sku(session: Session, product_id: uuid.UUID, sku_id: uuid.UUID) -> SKU:
    require_product(session, product_id)
    sku = session.get(SKU, sku_id)
    if sku is None or sku.product_id != product_id:
        raise UnknownProductDataError("SKU not found under this Product.")
    return sku


def get_product_data(session: Session, product_id: uuid.UUID) -> ProductDataSummary:
    product = require_product(session, product_id)
    assignments = {row.category_id: row for row in session.scalars(select(ProductCategory).where(ProductCategory.product_id == product_id)).all()}
    brands = session.scalars(select(Brand).order_by(Brand.name, Brand.id)).all()
    categories = list_active_categories(session)
    skus = session.scalars(select(SKU).where(SKU.product_id == product_id).order_by(SKU.created_at, SKU.id)).all()
    now = utc_now()
    def price_summary(price: Price) -> ProductDataPriceSummary:
        return ProductDataPriceSummary(
            price_id=price.id, amount=price.amount, currency=price.currency,
            valid_from=price.valid_from, source=price.source, approved=price.approved,
        )
    return ProductDataSummary(
        product=get_catalog_builder_product_summary(session, product_id=product_id),
        brand_id=product.brand_id,
        brands=[ProductDataBrandSummary(brand_id=brand.id, name=brand.name) for brand in brands],
        categories=[ProductDataCategorySummary(category_id=category.id, name=category.name, assigned=category.id in assignments, is_primary=assignments[category.id].is_primary if category.id in assignments else False) for category in categories],
        skus=[ProductDataSKUSummary(
            sku_id=sku.id, external_sku=sku.external_sku, flavor=sku.flavor,
            size_value=sku.size_value, size_unit=sku.size_unit, servings=sku.servings,
            active_price=(price_summary(active) if (active := select_active_approved_price(session, sku_id=sku.id, currency="PYG", as_of=now)) else None),
            price_history=[price_summary(price) for price in session.scalars(select(Price).where(Price.sku_id == sku.id).order_by(Price.valid_from.desc(), Price.created_at.desc(), Price.id.desc())).all()],
        ) for sku in skus],
        identity_history=[{
            "old_name": edit.old_name, "new_name": edit.new_name,
            "old_brand_id": str(edit.old_brand_id), "new_brand_id": str(edit.new_brand_id),
            "created_at": edit.created_at.isoformat(),
        } for edit in session.scalars(select(ProductIdentityEdit).where(ProductIdentityEdit.product_id == product_id).order_by(ProductIdentityEdit.created_at.desc(), ProductIdentityEdit.id.desc())).all()],
    )


def edit_product_identity(session: Session, product_id: uuid.UUID, request: ProductIdentityEditRequest) -> None:
    product = require_product(session, product_id)
    if session.get(Brand, request.brand_id) is None:
        raise UnknownProductDataError("Brand not found.")
    from app.domain.identity import identity_key_v1
    collision = session.scalar(select(Product).where(Product.brand_id == request.brand_id, Product.identity_key == identity_key_v1(request.name), Product.id != product_id))
    if collision is not None:
        raise ProductDataConflictError(f"Product identity already exists under this Brand: {collision.id}")
    if product.name == request.name and product.brand_id == request.brand_id:
        return
    edit = ProductIdentityEdit(product=product, old_name=product.name, new_name=request.name, old_brand_id=product.brand_id, new_brand_id=request.brand_id)
    product.name = request.name
    product.brand_id = request.brand_id
    session.add(edit)
    session.flush()


def edit_product_categories(session: Session, product_id: uuid.UUID, request: ProductCategoryEditRequest) -> None:
    require_product(session, product_id)
    desired = set(request.secondary_category_ids)
    if request.primary_category_id is not None:
        desired.add(request.primary_category_id)
    available = {category.id for category in list_active_categories(session)}
    if not desired <= available:
        raise UnknownProductDataError("Category not found or inactive.")
    current = session.scalars(select(ProductCategory).where(ProductCategory.product_id == product_id)).all()
    for assignment in current:
        if assignment.category_id not in desired:
            remove_product_category(session, product_id=product_id, category_id=assignment.category_id)
        elif assignment.is_primary and assignment.category_id != request.primary_category_id:
            assignment.is_primary = False
            session.flush()
    for category_id in sorted(desired, key=str):
        assignment = assign_product_category(session, product_id=product_id, category_id=category_id, is_primary=category_id == request.primary_category_id, replace_primary=True)
        assignment.source = FieldSource.HUMAN
        assignment.category_suggestion_run_id = None
        assignment.verified = True
        assignment.locked = True
    session.flush()


def edit_product_sku(session: Session, product_id: uuid.UUID, sku_id: uuid.UUID, request: ProductSKUEditRequest) -> None:
    sku = require_sku(session, product_id, sku_id)
    values = request.model_dump()
    candidate = SKUCreate(product_id=product_id, **values)
    matches = find_sku_variant_candidates(session, candidate)
    if any(other.id != sku_id for other in matches.exact + matches.external_sku):
        raise ProductDataConflictError("Another SKU already has this variant or external SKU.")
    for name, value in values.items():
        if getattr(sku, name) != value:
            apply_sku_field_update(session, sku=sku, field_name=SKUFieldName(name), value=value, source=FieldSource.HUMAN)
    session.flush()


def add_product_sku(session: Session, product_id: uuid.UUID, request: ProductSKUEditRequest) -> None:
    require_product(session, product_id)
    candidate = SKUCreate(product_id=product_id, **request.model_dump())
    matches = find_sku_variant_candidates(session, candidate)
    if matches.external_sku:
        raise ProductDataConflictError("External SKU already exists under this Product.")
    try:
        create_manual_sku(session, candidate)
    except DuplicateSKUVariantError as exc:
        raise ProductDataConflictError(str(exc)) from exc


def change_product_price(session: Session, product_id: uuid.UUID, sku_id: uuid.UUID, request: ProductPriceEditRequest) -> None:
    sku = require_sku(session, product_id, sku_id)
    now = utc_now()
    current = select_active_approved_price(session, sku_id=sku.id, currency=request.currency, as_of=now)
    if current is not None and current.amount == request.amount:
        return
    session.add(Price(sku=sku, amount=request.amount, currency=request.currency, valid_from=now, source="human", approved=True))
    session.flush()
