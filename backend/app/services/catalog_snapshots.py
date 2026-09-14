import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Brand,
    CatalogSnapshot,
    Category,
    DerivedImage,
    Photo,
    Price,
    Product,
    ProductCategory,
    SKU,
)
from app.db.types import utc_now
from app.domain.enums import (
    CatalogHeroPhotoSource,
    PhotoPresentationAssetType,
    PhotoRole,
)
from app.domain.schemas import (
    CatalogHeroSnapshot,
    CatalogProductSnapshot,
    CatalogSectionSnapshot,
    CatalogSnapshotCreate,
    CatalogSnapshotData,
    CatalogVariantSnapshot,
    FrozenCatalogAsset,
    FrozenCatalogCategory,
    FrozenCatalogPrice,
    ProductCatalogReadiness,
    SKUCatalogReadiness,
)
from app.domain.sku_variants import normalize_variant_text
from app.services.catalog_readiness import evaluate_product_catalog_readiness
from app.services.photo_intake import PhotoIntakeError, inspect_supported_image

CATALOG_SNAPSHOT_SCHEMA_VERSION = "catalog-snapshot-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STORAGE_ROOT = PROJECT_ROOT / "storage"
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class _FrozenProduct:
    category: FrozenCatalogCategory
    product: CatalogProductSnapshot


class CatalogSnapshotError(ValueError):
    pass


class CatalogSnapshotReadinessError(CatalogSnapshotError):
    def __init__(self, failures: list[ProductCatalogReadiness]):
        self.failures = tuple(failures)
        details = "; ".join(
            f"{report.product_id}:"
            + ",".join(issue.code.value for issue in report.blockers)
            for report in failures
        )
        super().__init__(f"selected Products are not catalog-ready: {details}")


class CatalogSnapshotSelectionError(CatalogSnapshotError):
    pass


class CatalogSnapshotConsistencyError(CatalogSnapshotError):
    pass


class CatalogSnapshotAssetIntegrityError(CatalogSnapshotError):
    pass


class UnknownCatalogSnapshotError(CatalogSnapshotError):
    pass


class CatalogSnapshotIntegrityError(CatalogSnapshotError):
    pass


def create_catalog_snapshot(
    session: Session,
    request: CatalogSnapshotCreate | Mapping[str, Any],
    *,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
    clock: Clock = utc_now,
) -> CatalogSnapshot:
    """Freeze one catalog-shaped publication payload without committing."""

    validated = CatalogSnapshotCreate.model_validate(request)
    resolved_as_of = _resolve_as_of(validated.as_of, clock)
    reports = [
        evaluate_product_catalog_readiness(
            session,
            product_id=product_id,
            currency=validated.currency,
            as_of=resolved_as_of,
        )
        for product_id in sorted(validated.product_ids, key=str)
    ]
    failures = [report for report in reports if not report.is_ready]
    if failures:
        raise CatalogSnapshotReadinessError(failures)

    products = [
        _freeze_product(
            session,
            report=report,
            selected_sku_ids=(
                validated.sku_selection.get(report.product_id)
                if validated.sku_selection is not None
                else None
            ),
            storage_root=storage_root,
        )
        for report in reports
    ]
    sections = _group_and_sort_sections(products)
    data = CatalogSnapshotData(
        schema_version=CATALOG_SNAPSHOT_SCHEMA_VERSION,
        currency=validated.currency,
        as_of=resolved_as_of,
        sections=sections,
    )
    payload = data.model_dump(mode="json")
    snapshot = CatalogSnapshot(
        schema_version=CATALOG_SNAPSHOT_SCHEMA_VERSION,
        currency=validated.currency,
        as_of=resolved_as_of,
        payload=payload,
        content_hash=hash_catalog_snapshot_data(data),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def read_catalog_snapshot_data(
    session: Session,
    snapshot_id: uuid.UUID,
) -> CatalogSnapshotData:
    """Validate stored snapshot content and hash without consulting live state."""

    snapshot = session.get(CatalogSnapshot, snapshot_id)
    if snapshot is None:
        raise UnknownCatalogSnapshotError(f"CatalogSnapshot not found: {snapshot_id}")
    if snapshot.schema_version != CATALOG_SNAPSHOT_SCHEMA_VERSION:
        raise CatalogSnapshotIntegrityError(
            f"unsupported CatalogSnapshot schema: {snapshot.schema_version}"
        )
    try:
        data = CatalogSnapshotData.model_validate(snapshot.payload)
    except ValidationError:
        raise CatalogSnapshotIntegrityError(
            "CatalogSnapshot payload does not match its schema"
        ) from None
    if (
        data.schema_version != snapshot.schema_version
        or data.currency != snapshot.currency
        or data.as_of.astimezone(timezone.utc)
        != snapshot.as_of.astimezone(timezone.utc)
    ):
        raise CatalogSnapshotIntegrityError(
            "CatalogSnapshot row metadata does not match its payload"
        )
    if hash_catalog_snapshot_data(data) != snapshot.content_hash.lower():
        raise CatalogSnapshotIntegrityError(
            "CatalogSnapshot payload failed content-hash verification"
        )
    return data


def canonical_catalog_snapshot_json(data: CatalogSnapshotData) -> str:
    return json.dumps(
        data.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def hash_catalog_snapshot_data(data: CatalogSnapshotData) -> str:
    return hashlib.sha256(
        canonical_catalog_snapshot_json(data).encode("utf-8")
    ).hexdigest()


def _freeze_product(
    session: Session,
    *,
    report: ProductCatalogReadiness,
    selected_sku_ids: list[uuid.UUID] | None,
    storage_root: Path,
) -> _FrozenProduct:
    product = session.get(Product, report.product_id)
    if product is None:
        raise CatalogSnapshotConsistencyError(
            f"ready Product disappeared: {report.product_id}"
        )
    brand = session.get(Brand, product.brand_id)
    category = session.get(Category, report.primary_category_id)
    if brand is None or category is None or not category.is_active:
        raise CatalogSnapshotConsistencyError(
            f"ready Product identity or primary Category changed: {product.id}"
        )
    primary = session.scalar(
        select(ProductCategory).where(
            ProductCategory.product_id == product.id,
            ProductCategory.category_id == category.id,
            ProductCategory.is_primary.is_(True),
        )
    )
    if primary is None:
        raise CatalogSnapshotConsistencyError(
            f"ready Product primary Category changed: {product.id}"
        )

    included_skus = _resolve_included_skus(
        session,
        product_id=product.id,
        report=report,
        selected_sku_ids=selected_sku_ids,
    )
    readiness_by_sku = {item.sku_id: item for item in report.sku_reports}
    variants = [
        _freeze_variant(
            session,
            sku=sku,
            readiness=readiness_by_sku[sku.id],
            currency=report.currency,
            as_of=report.as_of,
        )
        for sku in sorted(included_skus, key=_sku_sort_key)
    ]
    return _FrozenProduct(
        category=FrozenCatalogCategory(
            source_category_id=category.id,
            name=category.name,
            identity_key=category.identity_key,
            sort_order=category.sort_order,
        ),
        product=CatalogProductSnapshot(
            source_brand_id=brand.id,
            brand_name=brand.name,
            brand_identity_key=brand.identity_key,
            source_product_id=product.id,
            product_name=product.name,
            product_identity_key=product.identity_key,
            hero=_freeze_hero(
                session,
                product=product,
                report=report,
                storage_root=storage_root,
            ),
            variants=variants,
        ),
    )


def _resolve_included_skus(
    session: Session,
    *,
    product_id: uuid.UUID,
    report: ProductCatalogReadiness,
    selected_sku_ids: list[uuid.UUID] | None,
) -> list[SKU]:
    included_ids = report.ready_sku_ids if selected_sku_ids is None else selected_sku_ids
    if not included_ids:
        raise CatalogSnapshotSelectionError(
            f"Product requires at least one included SKU: {product_id}"
        )
    ready_ids = set(report.ready_sku_ids)
    included: list[SKU] = []
    for sku_id in included_ids:
        sku = session.get(SKU, sku_id)
        if sku is None:
            raise CatalogSnapshotSelectionError(f"selected SKU not found: {sku_id}")
        if sku.product_id != product_id:
            raise CatalogSnapshotSelectionError(
                f"selected SKU {sku_id} does not belong to Product {product_id}"
            )
        if sku.id not in ready_ids:
            raise CatalogSnapshotSelectionError(
                f"selected SKU is not publishable: {sku_id}"
            )
        included.append(sku)
    return included


def _freeze_variant(
    session: Session,
    *,
    sku: SKU,
    readiness: SKUCatalogReadiness,
    currency: str,
    as_of: datetime,
) -> CatalogVariantSnapshot:
    if not readiness.is_publishable or readiness.active_price_id is None:
        raise CatalogSnapshotConsistencyError(
            f"included SKU has no readiness-selected Price: {sku.id}"
        )
    price = session.get(Price, readiness.active_price_id)
    if (
        price is None
        or price.sku_id != sku.id
        or not price.approved
        or price.currency != currency
        or price.valid_from > as_of
        or price.amount != readiness.active_price_amount
        or price.currency != readiness.active_price_currency
        or price.valid_from != readiness.active_price_valid_from
    ):
        raise CatalogSnapshotConsistencyError(
            f"readiness-selected Price changed for SKU: {sku.id}"
        )
    return CatalogVariantSnapshot(
        source_sku_id=sku.id,
        external_sku=sku.external_sku,
        flavor=sku.flavor,
        size_value=sku.size_value,
        size_unit=sku.size_unit,
        servings=sku.servings,
        price=FrozenCatalogPrice(
            source_price_id=price.id,
            amount=price.amount,
            currency=price.currency,
            valid_from=price.valid_from,
            created_at=price.created_at,
            source=price.source,
        ),
    )


def _freeze_hero(
    session: Session,
    *,
    product: Product,
    report: ProductCatalogReadiness,
    storage_root: Path,
) -> CatalogHeroSnapshot:
    if report.presentation_warnings:
        raise CatalogSnapshotAssetIntegrityError(
            "selected hero presentation is not publication-valid: "
            + ",".join(item.value for item in report.presentation_warnings)
        )
    if (
        report.hero_photo_id is None
        or report.hero_photo_source is None
        or report.hero_presentation_type is None
    ):
        raise CatalogSnapshotConsistencyError(
            f"ready Product has no hero decision: {product.id}"
        )
    photo = session.get(Photo, report.hero_photo_id)
    if (
        photo is None
        or not photo.is_original
        or photo.role is not PhotoRole.FRONT
    ):
        raise CatalogSnapshotConsistencyError(
            f"ready Product source hero changed: {product.id}"
        )
    _verify_hero_ownership(session, product, photo, report)
    source_asset = _freeze_asset(
        file_path=photo.file_path,
        checksum_sha256=photo.checksum_sha256,
        persisted_mime_type=photo.mime_type,
        persisted_file_size=photo.file_size_bytes,
        persisted_width=photo.width,
        persisted_height=photo.height,
        storage_root=storage_root,
        expected_area="originals",
    )

    derived_id = report.hero_derived_image_id
    if report.hero_presentation_type is PhotoPresentationAssetType.ORIGINAL:
        if derived_id is not None:
            raise CatalogSnapshotConsistencyError(
                "original hero presentation unexpectedly references DerivedImage"
            )
        presentation_asset = source_asset
    else:
        if derived_id is None:
            raise CatalogSnapshotConsistencyError(
                "derived hero presentation is missing DerivedImage identity"
            )
        derived = session.get(DerivedImage, derived_id)
        if derived is None or derived.source_photo_id != photo.id:
            raise CatalogSnapshotConsistencyError(
                "readiness-selected DerivedImage lineage changed"
            )
        presentation_asset = _freeze_asset(
            file_path=derived.file_path,
            checksum_sha256=derived.checksum_sha256,
            persisted_mime_type=derived.mime_type,
            persisted_file_size=derived.file_size_bytes,
            persisted_width=derived.width,
            persisted_height=derived.height,
            storage_root=storage_root,
            expected_area="processed",
        )

    return CatalogHeroSnapshot(
        source_photo_id=photo.id,
        source_photo_owner_type=report.hero_photo_source,
        source_photo_owner_sku_id=report.hero_source_sku_id,
        source_original_asset=source_asset,
        presentation_type=report.hero_presentation_type,
        source_derived_image_id=derived_id,
        presentation_asset=presentation_asset,
    )


def _verify_hero_ownership(
    session: Session,
    product: Product,
    photo: Photo,
    report: ProductCatalogReadiness,
) -> None:
    if report.hero_photo_source is CatalogHeroPhotoSource.PRODUCT:
        valid = (
            photo.product_id == product.id
            and photo.sku_id is None
            and report.hero_source_sku_id is None
        )
    else:
        owner_sku = (
            session.get(SKU, report.hero_source_sku_id)
            if report.hero_source_sku_id is not None
            else None
        )
        valid = (
            photo.product_id is None
            and owner_sku is not None
            and owner_sku.product_id == product.id
            and photo.sku_id == owner_sku.id
        )
    if not valid:
        raise CatalogSnapshotConsistencyError(
            f"readiness hero ownership changed for Product: {product.id}"
        )


def _freeze_asset(
    *,
    file_path: str,
    checksum_sha256: str,
    persisted_mime_type: str | None,
    persisted_file_size: int | None,
    persisted_width: int | None,
    persisted_height: int | None,
    storage_root: Path,
    expected_area: str,
) -> FrozenCatalogAsset:
    path, relative_path = _canonical_asset_path(
        file_path,
        checksum_sha256=checksum_sha256,
        storage_root=storage_root,
        expected_area=expected_area,
    )
    try:
        content = path.read_bytes()
    except OSError:
        raise CatalogSnapshotAssetIntegrityError(
            f"selected {expected_area} asset is unavailable"
        ) from None
    checksum = hashlib.sha256(content).hexdigest()
    if checksum != checksum_sha256.lower():
        raise CatalogSnapshotAssetIntegrityError(
            f"selected {expected_area} asset failed checksum verification"
        )
    try:
        mime_type, extension, width, height = inspect_supported_image(content)
    except PhotoIntakeError:
        raise CatalogSnapshotAssetIntegrityError(
            f"selected {expected_area} asset is corrupt or unsupported"
        ) from None
    if path.suffix.lower() != extension:
        raise CatalogSnapshotAssetIntegrityError(
            f"selected {expected_area} asset extension is not canonical"
        )
    if (
        (persisted_mime_type is not None and persisted_mime_type != mime_type)
        or (persisted_file_size is not None and persisted_file_size != len(content))
        or (persisted_width is not None and persisted_width != width)
        or (persisted_height is not None and persisted_height != height)
    ):
        raise CatalogSnapshotAssetIntegrityError(
            f"selected {expected_area} asset metadata failed verification"
        )
    return FrozenCatalogAsset(
        checksum_sha256=checksum,
        mime_type=mime_type,
        file_size_bytes=len(content),
        width=width,
        height=height,
        storage_relative_path=relative_path,
    )


def _canonical_asset_path(
    file_path: str,
    *,
    checksum_sha256: str,
    storage_root: Path,
    expected_area: str,
) -> tuple[Path, str]:
    raw = Path(file_path)
    if ".." in raw.parts or "." in raw.parts:
        raise CatalogSnapshotAssetIntegrityError(
            "selected asset path contains traversal or non-canonical segments"
        )
    root = storage_root.resolve()
    if raw.is_absolute():
        resolved = raw.resolve()
    elif raw.parts and raw.parts[0] == root.name:
        resolved = (root.parent / raw).resolve()
    else:
        resolved = (root / raw).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise CatalogSnapshotAssetIntegrityError(
            "selected asset path is outside canonical storage"
        ) from None
    if (
        len(relative.parts) != 3
        or relative.parts[0] != expected_area
        or relative.parts[1] != checksum_sha256[:2].lower()
        or Path(relative.parts[2]).stem != checksum_sha256.lower()
    ):
        raise CatalogSnapshotAssetIntegrityError(
            f"selected {expected_area} asset path is not content-addressed"
        )
    return resolved, relative.as_posix()


def _group_and_sort_sections(
    products: list[_FrozenProduct],
) -> list[CatalogSectionSnapshot]:
    grouped: dict[
        uuid.UUID, tuple[FrozenCatalogCategory, list[CatalogProductSnapshot]]
    ] = {}
    for item in products:
        category = item.category
        if category.source_category_id not in grouped:
            grouped[category.source_category_id] = (category, [])
        grouped[category.source_category_id][1].append(item.product)

    sections = [
        CatalogSectionSnapshot(
            category=category,
            products=sorted(
                category_products,
                key=lambda item: (
                    item.brand_identity_key,
                    item.product_identity_key,
                    str(item.source_product_id),
                ),
            ),
        )
        for category, category_products in grouped.values()
    ]
    return sorted(
        sections,
        key=lambda item: (
            item.category.sort_order,
            item.category.identity_key,
            str(item.category.source_category_id),
        ),
    )


def _sku_sort_key(sku: SKU) -> tuple[Any, ...]:
    flavor = normalize_variant_text(sku.flavor)
    unit = normalize_variant_text(sku.size_unit)
    external_sku = normalize_variant_text(sku.external_sku)
    return (
        flavor is None,
        flavor or "",
        sku.size_value is None,
        sku.size_value or Decimal(0),
        unit is None,
        unit or "",
        sku.servings is None,
        sku.servings or 0,
        external_sku is None,
        external_sku or "",
        str(sku.id),
    )


def _resolve_as_of(requested: datetime | None, clock: Clock) -> datetime:
    resolved = requested if requested is not None else clock()
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise CatalogSnapshotError("snapshot as_of must be timezone-aware")
    return resolved.astimezone(timezone.utc)
