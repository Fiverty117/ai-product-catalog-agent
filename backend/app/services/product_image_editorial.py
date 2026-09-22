"""Read and mutate existing Product image review/presentation state."""
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DerivedImage, Photo, Product, SKU
from app.domain.enums import DerivedImageReviewState, PhotoPresentationAssetType
from app.domain.schemas import (
    DerivedImageReviewCreate, ProductDerivedImageReviewRequest, ProductImageDerivedSummary,
    ProductImageEditorialSummary, ProductImageEffectiveSummary,
    ProductImagePresentationRequest,
)
from app.services.catalog_builder import get_catalog_builder_product_summary
from app.services.catalog_readiness import resolve_catalog_hero_source
from app.services.image_presentation import (
    create_derived_image_review, get_derived_image_review_state,
    resolve_effective_photo_presentation, select_derived_image_for_photo,
    use_original_photo_presentation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ProductImageEditorialError(ValueError):
    pass


class UnknownProductImageResourceError(ProductImageEditorialError):
    pass


class ProductImageLifecycleConflictError(ProductImageEditorialError):
    pass


class ProductImageAssetUnavailableError(ProductImageEditorialError):
    pass


def get_product_image_editorial_summary(session: Session, product_id: uuid.UUID) -> ProductImageEditorialSummary:
    if session.get(Product, product_id) is None:
        raise UnknownProductImageResourceError("Product not found.")
    photo, owner, owner_sku_id, _ = resolve_catalog_hero_source(session, product_id)
    product = get_catalog_builder_product_summary(session, product_id=product_id)
    if photo is None:
        return ProductImageEditorialSummary(
            product_id=product_id, source_photo_id=None, source_owner=None,
            source_sku_id=None, original_preview_url=None, effective=None,
            derived_images=[], product=product,
        )
    effective = resolve_effective_photo_presentation(session, photo_id=photo.id)
    derived = session.scalars(
        select(DerivedImage).where(DerivedImage.source_photo_id == photo.id)
        .order_by(DerivedImage.created_at.desc(), DerivedImage.id.desc())
    ).all()
    return ProductImageEditorialSummary(
        product_id=product_id, source_photo_id=photo.id,
        source_owner=owner.value, source_sku_id=owner_sku_id,
        original_preview_url=f"/api/products/{product_id}/images/photos/{photo.id}",
        effective=ProductImageEffectiveSummary(
            presentation=effective.asset_type,
            derived_image_id=effective.derived_image_id,
            preview_url=(
                f"/api/products/{product_id}/images/derived/{effective.derived_image_id}"
                if effective.asset_type is PhotoPresentationAssetType.DERIVED
                else f"/api/products/{product_id}/images/photos/{photo.id}"
            ),
            warnings=effective.warnings,
        ),
        derived_images=[_derived_summary(session, product_id, row, effective.derived_image_id) for row in derived],
        product=product,
    )


def review_product_derived_image(session: Session, product_id: uuid.UUID, derived_image_id: uuid.UUID, request: ProductDerivedImageReviewRequest) -> None:
    derived = _require_product_derived(session, product_id, derived_image_id)
    if get_derived_image_review_state(session, derived.id) is not DerivedImageReviewState.UNREVIEWED:
        raise ProductImageLifecycleConflictError("Derived image has already been reviewed.")
    create_derived_image_review(session, DerivedImageReviewCreate(derived_image_id=derived.id, decision=request.decision))


def select_product_image_presentation(session: Session, product_id: uuid.UUID, photo_id: uuid.UUID, request: ProductImagePresentationRequest) -> None:
    photo = _require_product_photo(session, product_id, photo_id)
    if request.presentation == "original":
        use_original_photo_presentation(session, photo_id=photo.id)
        return
    derived = _require_product_derived(session, product_id, request.derived_image_id)
    if derived.source_photo_id != photo.id:
        raise ProductImageLifecycleConflictError("Derived image does not belong to the selected source Photo.")
    select_derived_image_for_photo(session, photo_id=photo.id, derived_image_id=derived.id)


def resolve_product_photo_asset(session: Session, product_id: uuid.UUID, photo_id: uuid.UUID) -> tuple[Path, str]:
    photo = _require_product_photo(session, product_id, photo_id)
    return _available_path(photo.file_path), photo.mime_type


def resolve_product_derived_asset(session: Session, product_id: uuid.UUID, derived_image_id: uuid.UUID) -> tuple[Path, str]:
    derived = _require_product_derived(session, product_id, derived_image_id)
    return _available_path(derived.file_path), derived.mime_type


def _derived_summary(session: Session, product_id: uuid.UUID, derived: DerivedImage, selected_id: uuid.UUID | None) -> ProductImageDerivedSummary:
    state = get_derived_image_review_state(session, derived.id)
    available = _path(derived.file_path).is_file()
    return ProductImageDerivedSummary(
        derived_image_id=derived.id, review_state=state,
        selectable=state is DerivedImageReviewState.APPROVED and available,
        asset_available=available, selected=derived.id == selected_id,
        preview_url=f"/api/products/{product_id}/images/derived/{derived.id}" if available else None,
        created_at=derived.created_at,
    )


def _require_product_photo(session: Session, product_id: uuid.UUID, photo_id: uuid.UUID) -> Photo:
    if session.get(Product, product_id) is None:
        raise UnknownProductImageResourceError("Product not found.")
    photo = session.get(Photo, photo_id)
    if photo is None or not _photo_belongs_to_product(session, photo, product_id):
        raise UnknownProductImageResourceError("Photo not found under this Product.")
    return photo


def _require_product_derived(session: Session, product_id: uuid.UUID, derived_image_id: uuid.UUID | None) -> DerivedImage:
    if derived_image_id is None:
        raise UnknownProductImageResourceError("Derived image not found.")
    derived = session.get(DerivedImage, derived_image_id)
    if derived is None:
        raise UnknownProductImageResourceError("Derived image not found.")
    _require_product_photo(session, product_id, derived.source_photo_id)
    return derived


def _photo_belongs_to_product(session: Session, photo: Photo, product_id: uuid.UUID) -> bool:
    if photo.product_id == product_id:
        return True
    return photo.sku_id is not None and session.scalar(select(SKU.product_id).where(SKU.id == photo.sku_id)) == product_id


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _available_path(value: str) -> Path:
    path = _path(value)
    if not path.is_file():
        raise ProductImageAssetUnavailableError("Image asset is unavailable.")
    return path
