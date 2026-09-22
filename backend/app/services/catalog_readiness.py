import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, Photo, Product, ProductCategory, SKU
from app.db.types import utc_now
from app.domain.enums import (
    CatalogHeroPhotoSource,
    CatalogReadinessIssueCode,
    CatalogReadinessIssueScope,
    CatalogReadinessIssueSeverity,
    PhotoRole,
)
from app.domain.schemas import (
    CatalogReadinessIssue,
    ProductCatalogReadiness,
    ProductCatalogReadinessRequest,
    SKUCatalogReadiness,
)
from app.services.image_presentation import resolve_effective_photo_presentation
from app.services.prices import select_active_approved_price

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class CatalogReadinessError(ValueError):
    pass


class UnknownCatalogReadinessProductError(CatalogReadinessError):
    pass


def evaluate_product_catalog_readiness(
    session: Session,
    *,
    product_id: uuid.UUID,
    currency: str,
    as_of: datetime | None = None,
) -> ProductCatalogReadiness:
    """Derive current publishability without mutating canonical or workflow state."""

    request = ProductCatalogReadinessRequest(
        product_id=product_id,
        currency=currency,
        as_of=as_of,
    )
    effective_as_of = (request.as_of or utc_now()).astimezone(timezone.utc)
    product = session.get(Product, request.product_id)
    if product is None:
        raise UnknownCatalogReadinessProductError(
            f"Product not found: {request.product_id}"
        )

    blockers: list[CatalogReadinessIssue] = []
    warnings: list[CatalogReadinessIssue] = []
    primary_category_id = _evaluate_categories(
        session,
        product,
        blockers=blockers,
        warnings=warnings,
    )

    skus = list(
        session.scalars(
            select(SKU).where(SKU.product_id == product.id).order_by(SKU.id)
        ).all()
    )
    if not skus:
        blockers.append(
            _issue(
                CatalogReadinessIssueCode.NO_SKUS,
                CatalogReadinessIssueSeverity.BLOCKER,
                CatalogReadinessIssueScope.PRODUCT,
                "Product has no SKUs.",
                product_id=product.id,
            )
        )

    hero_photo, hero_source, hero_source_sku_id, has_front_records = (
        resolve_catalog_hero_source(session, product.id)
    )
    if hero_photo is None:
        blockers.append(
            _issue(
                (
                    CatalogReadinessIssueCode.MISSING_CATALOG_PHOTO_ASSET
                    if has_front_records
                    else CatalogReadinessIssueCode.MISSING_CATALOG_FRONT_PHOTO
                ),
                CatalogReadinessIssueSeverity.BLOCKER,
                CatalogReadinessIssueScope.PRODUCT,
                (
                    "Catalog front Photo records exist, but no backing asset is available."
                    if has_front_records
                    else "Product has no eligible front Photo."
                ),
                product_id=product.id,
            )
        )

    hero_presentation = (
        resolve_effective_photo_presentation(session, photo_id=hero_photo.id)
        if hero_photo is not None
        else None
    )

    sku_reports: list[SKUCatalogReadiness] = []
    ready_sku_ids: list[uuid.UUID] = []
    for sku in skus:
        active_price = select_active_approved_price(
            session,
            sku_id=sku.id,
            currency=request.currency,
            as_of=effective_as_of,
        )
        sku_blockers: list[CatalogReadinessIssue] = []
        if active_price is None:
            sku_blockers.append(
                _issue(
                    CatalogReadinessIssueCode.MISSING_ACTIVE_APPROVED_PRICE,
                    CatalogReadinessIssueSeverity.BLOCKER,
                    CatalogReadinessIssueScope.SKU,
                    f"SKU has no active approved {request.currency} price.",
                    product_id=product.id,
                    sku_id=sku.id,
                )
            )
            warnings.append(
                _issue(
                    CatalogReadinessIssueCode.SKU_EXCLUDED_MISSING_ACTIVE_PRICE,
                    CatalogReadinessIssueSeverity.WARNING,
                    CatalogReadinessIssueScope.SKU,
                    "SKU will be excluded because it has no active approved "
                    f"{request.currency} price.",
                    product_id=product.id,
                    sku_id=sku.id,
                )
            )
        else:
            ready_sku_ids.append(sku.id)
        sku_reports.append(
            SKUCatalogReadiness(
                sku_id=sku.id,
                is_publishable=active_price is not None,
                active_price_id=active_price.id if active_price is not None else None,
                active_price_amount=(
                    active_price.amount if active_price is not None else None
                ),
                active_price_currency=(
                    active_price.currency if active_price is not None else None
                ),
                active_price_valid_from=(
                    active_price.valid_from if active_price is not None else None
                ),
                blockers=sku_blockers,
            )
        )

    if skus and not ready_sku_ids:
        blockers.append(
            _issue(
                CatalogReadinessIssueCode.NO_PUBLISHABLE_SKUS,
                CatalogReadinessIssueSeverity.BLOCKER,
                CatalogReadinessIssueScope.PRODUCT,
                f"Product has no publishable SKUs for {request.currency}.",
                product_id=product.id,
            )
        )

    return ProductCatalogReadiness(
        product_id=product.id,
        currency=request.currency,
        as_of=effective_as_of,
        is_ready=not blockers,
        primary_category_id=primary_category_id,
        hero_photo_id=hero_photo.id if hero_photo is not None else None,
        hero_photo_source=hero_source,
        hero_source_sku_id=hero_source_sku_id,
        hero_presentation_type=(
            hero_presentation.asset_type if hero_presentation is not None else None
        ),
        hero_derived_image_id=(
            hero_presentation.derived_image_id
            if hero_presentation is not None
            else None
        ),
        presentation_warnings=(
            hero_presentation.warnings if hero_presentation is not None else []
        ),
        ready_sku_ids=ready_sku_ids,
        sku_reports=sku_reports,
        blockers=blockers,
        warnings=warnings,
    )


def _evaluate_categories(
    session: Session,
    product: Product,
    *,
    blockers: list[CatalogReadinessIssue],
    warnings: list[CatalogReadinessIssue],
) -> uuid.UUID | None:
    rows = session.execute(
        select(ProductCategory, Category)
        .join(Category, Category.id == ProductCategory.category_id)
        .where(ProductCategory.product_id == product.id)
        .order_by(Category.sort_order, Category.identity_key, Category.id)
    ).all()
    primary = next(
        (
            (assignment, category)
            for assignment, category in rows
            if assignment.is_primary
        ),
        None,
    )
    if primary is None:
        blockers.append(
            _issue(
                CatalogReadinessIssueCode.MISSING_PRIMARY_CATEGORY,
                CatalogReadinessIssueSeverity.BLOCKER,
                CatalogReadinessIssueScope.PRODUCT,
                "Product has no primary Category.",
                product_id=product.id,
            )
        )
        primary_category_id = None
    else:
        _, primary_category = primary
        primary_category_id = primary_category.id
        if not primary_category.is_active:
            blockers.append(
                _issue(
                    CatalogReadinessIssueCode.INACTIVE_PRIMARY_CATEGORY,
                    CatalogReadinessIssueSeverity.BLOCKER,
                    CatalogReadinessIssueScope.CATEGORY,
                    "Product primary Category is inactive.",
                    product_id=product.id,
                    category_id=primary_category.id,
                )
            )

    for assignment, category in rows:
        if not assignment.is_primary and not category.is_active:
            warnings.append(
                _issue(
                    CatalogReadinessIssueCode.INACTIVE_SECONDARY_CATEGORY,
                    CatalogReadinessIssueSeverity.WARNING,
                    CatalogReadinessIssueScope.CATEGORY,
                    "Product secondary Category is inactive.",
                    product_id=product.id,
                    category_id=category.id,
                )
            )
    return primary_category_id


def resolve_catalog_hero_source(
    session: Session,
    product_id: uuid.UUID,
) -> tuple[Photo | None, CatalogHeroPhotoSource | None, uuid.UUID | None, bool]:
    product_photos = list(
        session.scalars(
            select(Photo)
            .where(
                Photo.product_id == product_id,
                Photo.role == PhotoRole.FRONT,
            )
            .order_by(Photo.created_at, Photo.id)
        ).all()
    )
    sku_photo_rows = session.execute(
        select(Photo, SKU.id.label("owner_sku_id"))
        .join(SKU, SKU.id == Photo.sku_id)
        .where(
            SKU.product_id == product_id,
            Photo.role == PhotoRole.FRONT,
        )
        .order_by(SKU.id, Photo.created_at, Photo.id)
    ).all()
    for photo in product_photos:
        if _photo_asset_is_available(photo):
            return photo, CatalogHeroPhotoSource.PRODUCT, None, True
    for photo, owner_sku_id in sku_photo_rows:
        if _photo_asset_is_available(photo):
            return photo, CatalogHeroPhotoSource.SKU, owner_sku_id, True
    return None, None, None, bool(product_photos or sku_photo_rows)


def _photo_asset_is_available(photo: Photo) -> bool:
    path = Path(photo.file_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.is_file()


def _issue(
    code: CatalogReadinessIssueCode,
    severity: CatalogReadinessIssueSeverity,
    scope: CatalogReadinessIssueScope,
    message: str,
    *,
    product_id: uuid.UUID | None = None,
    sku_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    photo_id: uuid.UUID | None = None,
) -> CatalogReadinessIssue:
    return CatalogReadinessIssue(
        code=code,
        severity=severity,
        scope=scope,
        message=message,
        product_id=product_id,
        sku_id=sku_id,
        category_id=category_id,
        photo_id=photo_id,
    )
