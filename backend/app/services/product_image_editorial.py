"""Read and mutate existing Product image review/presentation state."""
import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DerivedImage, Job, Photo, Product, SKU
from app.domain.enums import DerivedImageReviewState, JobStatus, PhotoPresentationAssetType
from app.domain.schemas import (
    DerivedImageReviewCreate, ProductDerivedImageReviewRequest, ProductImageDerivedSummary,
    ProductImageEditorialSummary, ProductImageEffectiveSummary,
    ProductImageGenerationSummary, ProductImagePresentationRequest,
)
from app.services.catalog_builder import get_catalog_builder_product_summary
from app.services.catalog_readiness import resolve_catalog_hero_source
from app.services.image_presentation import (
    create_derived_image_review, get_derived_image_review_state,
    resolve_effective_photo_presentation, select_derived_image_for_photo,
    use_original_photo_presentation,
)
from app.services.image_enhancement import IMAGE_ENHANCEMENT_JOB_TYPE, enqueue_image_enhancement
from app.services.jobs import JobRecoveryError, requeue_failed_job
from app.services.photo_intake import PhotoIntakeError, inspect_supported_image

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
    jobs = _photo_generation_jobs(session, photo.id)
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
        generations=[image_generation_summary(job) for job in jobs],
        can_generate=_source_valid(photo) and not any(_active(job) for job in jobs),
        product=product,
    )


def enqueue_product_image_generation(session: Session, product_id: uuid.UUID, photo_id: uuid.UUID, request_key: str) -> Job:
    photo = _require_current_source(session, product_id, photo_id)
    if not _source_valid(photo):
        raise ProductImageLifecycleConflictError("Source Photo is unavailable or invalid.")
    active = next((job for job in _photo_generation_jobs(session, photo.id) if _active(job)), None)
    job = enqueue_image_enhancement(session, source_photo_id=photo.id, generation_request_key=request_key)
    if active is not None and active.id != job.id:
        raise ProductImageLifecycleConflictError("An image enhancement is already in progress for this Photo.")
    return job


def get_product_image_generation(session: Session, product_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None or job.job_type != IMAGE_ENHANCEMENT_JOB_TYPE:
        raise UnknownProductImageResourceError("Image enhancement not found.")
    try:
        photo_id = uuid.UUID(str(job.payload.get("source_photo_id", "")))
    except ValueError:
        raise UnknownProductImageResourceError("Image enhancement not found.") from None
    _require_product_photo(session, product_id, photo_id)
    return job


def retry_product_image_generation(session: Session, product_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = get_product_image_generation(session, product_id, job_id)
    if _active(job):
        return job
    if job.status is not JobStatus.FAILED:
        raise ProductImageLifecycleConflictError("Only a failed image enhancement can be retried.")
    photo_id = uuid.UUID(str(job.payload["source_photo_id"]))
    _require_current_source(session, product_id, photo_id)
    if not _source_valid(session.get(Photo, photo_id)):
        raise ProductImageLifecycleConflictError("Source Photo is unavailable or invalid.")
    active = next((other for other in _photo_generation_jobs(session, photo_id) if _active(other)), None)
    if job.attempts < job.max_attempts:
        if active is not None:
            raise ProductImageLifecycleConflictError("An image enhancement is already in progress for this Photo.")
        try:
            return requeue_failed_job(session, job)
        except JobRecoveryError as exc:
            raise ProductImageLifecycleConflictError(str(exc)) from exc
    replacement = enqueue_image_enhancement(
        session, source_photo_id=photo_id,
        provider=job.payload["provider"], model=job.payload["model"],
        prompt_version=job.payload["prompt_version"], config_version=job.payload["config_version"],
        parameters=job.payload["parameters"], generation_request_key=f"retry:{job.id}",
    )
    if active is not None and active.id != replacement.id:
        raise ProductImageLifecycleConflictError("An image enhancement is already in progress for this Photo.")
    if replacement.status is JobStatus.FAILED:
        return retry_product_image_generation(session, product_id, replacement.id)
    return replacement


def image_generation_summary(job: Job) -> ProductImageGenerationSummary:
    return ProductImageGenerationSummary(
        job_id=job.id, status=job.status, attempts=job.attempts,
        max_attempts=job.max_attempts, can_retry=job.status is JobStatus.FAILED,
        created_at=job.created_at,
    )


def _active(job: Job) -> bool:
    return job.status in (JobStatus.QUEUED, JobStatus.RUNNING)


def _photo_generation_jobs(session: Session, photo_id: uuid.UUID) -> list[Job]:
    jobs = session.scalars(select(Job).where(Job.job_type == IMAGE_ENHANCEMENT_JOB_TYPE).order_by(Job.created_at.desc(), Job.id.desc())).all()
    return [job for job in jobs if job.payload.get("source_photo_id") == str(photo_id)]


def _require_current_source(session: Session, product_id: uuid.UUID, photo_id: uuid.UUID) -> Photo:
    photo = _require_product_photo(session, product_id, photo_id)
    current, _, _, _ = resolve_catalog_hero_source(session, product_id)
    if current is None or current.id != photo.id or not photo.is_original:
        raise ProductImageLifecycleConflictError("Photo is not the current original front source.")
    return photo


def _source_valid(photo: Photo) -> bool:
    try:
        content = _path(photo.file_path).read_bytes()
        mime, _, width, height = inspect_supported_image(content)
        return (hashlib.sha256(content).hexdigest() == photo.checksum_sha256.lower()
                and mime == photo.mime_type and width == photo.width and height == photo.height
                and len(content) == photo.file_size_bytes)
    except (OSError, PhotoIntakeError):
        return False


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
