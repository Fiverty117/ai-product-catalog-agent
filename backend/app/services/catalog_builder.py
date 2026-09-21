import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Brand, CatalogBrandProfile, Category, Product, SKU
from app.db.types import utc_now
from app.domain.enums import ProductCopyResolutionState
from app.domain.schemas import (
    CatalogBuilderBrandProfileSummary,
    CatalogBuilderHeroSummary,
    CatalogBuilderLayoutSummary,
    CatalogBuilderProductList,
    CatalogBuilderProductSummary,
    CatalogBuilderReadinessSummary,
    CatalogBuilderSKUSummary,
)
from app.rendering.catalog_layouts import catalog_layout_definitions
from app.services.catalog_readiness import evaluate_product_catalog_readiness
from app.services.image_presentation import resolve_effective_photo_presentation
from app.services.product_copy_review import resolve_effective_product_copy

CatalogBuilderReadinessFilter = Literal["all", "ready", "not_ready"]

_LAYOUT_LABELS = {
    "classic": "Classic",
    "dense": "Dense",
    "compact": "Compact",
}


class UnknownCatalogBuilderProductError(ValueError):
    pass


class CatalogBuilderImageUnavailableError(ValueError):
    pass


def list_catalog_builder_products(
    session: Session,
    *,
    currency: str = "PYG",
    search: str | None = None,
    readiness: CatalogBuilderReadinessFilter = "all",
    as_of: datetime | None = None,
) -> CatalogBuilderProductList:
    """Compose the narrow read-only Product model needed by Catalog Builder."""

    effective_as_of = (as_of or utc_now()).astimezone(timezone.utc)
    query = select(Product).join(Brand, Brand.id == Product.brand_id)
    normalized_search = " ".join((search or "").split()).lower()
    if normalized_search:
        query = query.where(
            or_(
                func.lower(Product.name).contains(normalized_search),
                func.lower(Brand.name).contains(normalized_search),
            )
        )
    products = session.scalars(
        query.order_by(
            func.lower(Brand.name),
            func.lower(Product.name),
            Product.id,
        )
    ).all()

    summaries = [
        _build_product_summary(
            session,
            product=product,
            currency=currency,
            as_of=effective_as_of,
        )
        for product in products
    ]
    if readiness == "ready":
        summaries = [item for item in summaries if item.readiness.ready]
    elif readiness == "not_ready":
        summaries = [item for item in summaries if not item.readiness.ready]

    return CatalogBuilderProductList(
        currency=currency,
        as_of=effective_as_of,
        products=summaries,
    )


def list_catalog_builder_brand_profiles(
    session: Session,
) -> list[CatalogBuilderBrandProfileSummary]:
    profiles = session.scalars(
        select(CatalogBrandProfile)
        .where(CatalogBrandProfile.is_active.is_(True))
        .order_by(
            func.lower(CatalogBrandProfile.display_name),
            CatalogBrandProfile.key,
            CatalogBrandProfile.id,
        )
    ).all()
    return [
        CatalogBuilderBrandProfileSummary(
            id=profile.id,
            key=profile.key,
            display_name=profile.display_name,
            logo_url=(
                f"/api/catalog-builder/brand-profiles/{profile.id}/logo"
                if profile.logo_asset_id is not None
                else None
            ),
            primary_color=profile.primary_color,
            accent_color=profile.accent_color,
        )
        for profile in profiles
    ]


def list_catalog_builder_layouts() -> list[CatalogBuilderLayoutSummary]:
    return [
        CatalogBuilderLayoutSummary(
            key=layout.key,
            version=layout.version,
            display_label=_LAYOUT_LABELS[layout.key],
            products_per_row=layout.products_per_row,
            page_size=layout.page_size,
            orientation=layout.orientation,
        )
        for layout in catalog_layout_definitions()
    ]


def get_catalog_builder_product_summary(
    session: Session,
    *,
    product_id: uuid.UUID,
    currency: str = "PYG",
    as_of: datetime | None = None,
) -> CatalogBuilderProductSummary:
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownCatalogBuilderProductError(f"Product not found: {product_id}")
    return _build_product_summary(
        session,
        product=product,
        currency=currency,
        as_of=(as_of or utc_now()).astimezone(timezone.utc),
    )


def resolve_catalog_builder_product_image(
    session: Session,
    *,
    product_id: uuid.UUID,
) -> tuple[Path, str]:
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownCatalogBuilderProductError(f"Product not found: {product_id}")
    readiness = evaluate_product_catalog_readiness(
        session,
        product_id=product_id,
        currency="PYG",
    )
    if readiness.hero_photo_id is None:
        raise CatalogBuilderImageUnavailableError("Product has no effective image")
    presentation = resolve_effective_photo_presentation(
        session,
        photo_id=readiness.hero_photo_id,
    )
    path = Path(presentation.file_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    if not path.is_file():
        raise CatalogBuilderImageUnavailableError("Product image is unavailable")
    return path, presentation.mime_type


def _build_product_summary(
    session: Session,
    *,
    product: Product,
    currency: str,
    as_of: datetime,
) -> CatalogBuilderProductSummary:
    readiness = evaluate_product_catalog_readiness(
        session,
        product_id=product.id,
        currency=currency,
        as_of=as_of,
    )
    category = (
        session.get(Category, readiness.primary_category_id)
        if readiness.primary_category_id is not None
        else None
    )
    skus = {
        sku.id: sku
        for sku in session.scalars(
            select(SKU).where(SKU.product_id == product.id).order_by(SKU.id)
        ).all()
    }
    publishable_skus: list[CatalogBuilderSKUSummary] = []
    for report in readiness.sku_reports:
        if not report.is_publishable:
            continue
        sku = skus[report.sku_id]
        publishable_skus.append(
            CatalogBuilderSKUSummary(
                sku_id=sku.id,
                variant_label=_variant_label(sku),
                flavor=sku.flavor,
                size_value=sku.size_value,
                size_unit=sku.size_unit,
                active_price_amount=report.active_price_amount,
                currency=report.active_price_currency,
            )
        )

    hero = None
    if (
        readiness.hero_photo_id is not None
        and readiness.hero_presentation_type is not None
    ):
        hero = CatalogBuilderHeroSummary(
            source_photo_id=readiness.hero_photo_id,
            effective_derived_image_id=readiness.hero_derived_image_id,
            presentation_type=readiness.hero_presentation_type,
            image_url=f"/api/catalog-builder/products/{product.id}/image",
        )

    effective_copy = resolve_effective_product_copy(session, product.id)
    return CatalogBuilderProductSummary(
        product_id=product.id,
        product_name=product.name,
        brand_name=product.brand.name,
        primary_category_id=category.id if category is not None else None,
        primary_category_name=category.name if category is not None else None,
        readiness=CatalogBuilderReadinessSummary(
            ready=readiness.is_ready,
            blockers=readiness.blockers,
            warnings=readiness.warnings,
            presentation_warnings=readiness.presentation_warnings,
        ),
        publishable_skus=publishable_skus,
        hero=hero,
        copy_state=effective_copy.state,
        short_description=(
            effective_copy.short_description
            if effective_copy.state is ProductCopyResolutionState.CURRENT
            else None
        ),
    )


def _variant_label(sku: SKU) -> str:
    pieces: list[str] = []
    if sku.flavor:
        pieces.append(sku.flavor)
    if sku.size_value is not None and sku.size_unit is not None:
        size = format(sku.size_value.normalize(), "f")
        pieces.append(f"{size} {sku.size_unit}")
    return " / ".join(pieces) or sku.external_sku or "Standard"
