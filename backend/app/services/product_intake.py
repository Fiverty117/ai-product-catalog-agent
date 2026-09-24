"""Non-canonical product intake; extraction runs remain immutable observations."""

import hashlib
import uuid
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.openai_vision import OPENAI_PROVIDER, configured_openai_vision_model
from app.ai.prompts.product_extraction_v1 import PROMPT_VERSION
from app.db.models import ExtractionRun, Job, Photo, ProductIntakeItem, ProductIntakePhoto
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus, JobStatus
from app.domain.product_intake import (
    IntakeExtractionRead, IntakePhotoRead, IntakePromotionLink, ProductIntakeDraft, ProductIntakeRead, IntakeSKU,
)
from app.domain.schemas import ProductExtractionJobPayload, ProductExtractionResult
from app.services.extraction import PRODUCT_EXTRACTION_JOB_TYPE, PRODUCT_EXTRACTION_SCHEMA_VERSION
from app.services.jobs import enqueue_job
from app.services.photo_intake import inspect_supported_image, register_original_photo


class UnknownIntakeError(ValueError):
    pass


class IntakeConflictError(ValueError):
    pass


class IntakeInputError(ValueError):
    pass


class IntakeAssetError(ValueError):
    pass


def create_intake(session: Session) -> ProductIntakeItem:
    item = ProductIntakeItem(status="draft", draft=ProductIntakeDraft().model_dump(mode="json"))
    session.add(item)
    session.flush()
    return item


def require_intake(session: Session, intake_id: uuid.UUID) -> ProductIntakeItem:
    item = session.get(ProductIntakeItem, intake_id)
    if item is None:
        raise UnknownIntakeError("Intake item not found.")
    return item


def add_photo(session: Session, item: ProductIntakeItem, *, image_bytes: bytes, filename: str, originals_dir: Path) -> None:
    _require_not_promoted(item)
    if len(item.photos) >= 12:
        raise IntakeInputError("An intake item supports at most 12 photos.")
    if len(image_bytes) > 20 * 1024 * 1024:
        raise IntakeInputError("Each photo must be 20 MB or smaller.")
    photo = register_original_photo(
        session, image_bytes=image_bytes, original_filename=filename,
        originals_dir=originals_dir,
    )
    position = len(item.photos)
    association = ProductIntakePhoto(
        intake_item_id=item.id, photo=photo, position=position, is_primary=position == 0,
    )
    session.add(association)
    item.photos.append(association)
    item.updated_at = utc_now()
    session.flush()


def set_primary_photo(session: Session, item: ProductIntakeItem, photo_id: uuid.UUID) -> None:
    _require_not_promoted(item)
    if not any(link.photo_id == photo_id for link in item.photos):
        raise IntakeConflictError("Photo is not attached to this intake item.")
    for link in item.photos:
        link.is_primary = link.photo_id == photo_id
    item.updated_at = utc_now()
    session.flush()


def save_draft(session: Session, item: ProductIntakeItem, draft: ProductIntakeDraft) -> None:
    _require_not_promoted(item)
    item.draft = draft.model_dump(mode="json")
    item.human_edited = True
    item.updated_at = utc_now()
    session.flush()


def enqueue_extraction(session: Session, item: ProductIntakeItem, action_key: uuid.UUID) -> None:
    _require_not_promoted(item)
    _refresh_status(session, item)
    key = str(action_key)
    if item.latest_action_key == key:
        return
    if session.scalar(select(Job.id).where(Job.idempotency_key == f"product-intake:{item.id}:{key}")) is not None:
        raise IntakeConflictError("This idempotency key belongs to an earlier extraction action.")
    if item.status in {"queued", "running"}:
        raise IntakeConflictError("An extraction is already active for this intake item.")
    if not item.photos:
        raise IntakeInputError("Upload at least one source photo before extraction.")
    ordered = sorted(item.photos, key=lambda link: (not link.is_primary, link.position))
    payload = ProductExtractionJobPayload(
        photo_ids=[link.photo_id for link in ordered],
        provider=OPENAI_PROVIDER,
        model=configured_openai_vision_model(),
        prompt_version=PROMPT_VERSION,
        schema_version=PRODUCT_EXTRACTION_SCHEMA_VERSION,
        parameters={"reasoning_effort": "low", "image_detail": "high"},
    )
    job = enqueue_job(
        session, job_type=PRODUCT_EXTRACTION_JOB_TYPE,
        payload=payload.model_dump(mode="json"),
        idempotency_key=f"product-intake:{item.id}:{key}",
    )
    item.latest_job_id = job.id
    item.latest_run_id = None
    item.latest_action_key = key
    item.status = "queued"
    item.updated_at = utc_now()
    session.flush()


def _draft_from_observation(result: ProductExtractionResult) -> ProductIntakeDraft:
    def observed(field):
        return field.value if field.state.value == "extracted" else None

    size_value = observed(result.size_value)
    size_unit = observed(result.size_unit)
    sku = IntakeSKU(
        flavor=observed(result.flavor),
        size_value=size_value if size_unit is not None else None,
        size_unit=size_unit if size_value is not None else None,
        servings=observed(result.servings),
    ) if (observed(result.flavor) is not None or (
        size_value is not None and size_unit is not None
    ) or observed(result.servings) is not None) else None
    # An incomplete observed size pair stays in the immutable run; it is not a valid editable SKU.
    return ProductIntakeDraft(
        brand_name=observed(result.brand_name), product_name=observed(result.product_name),
        skus=[sku] if sku else [],
    )


def _refresh_status(session: Session, item: ProductIntakeItem) -> None:
    if item.promotion is not None:
        if item.status != "promoted":
            item.status = "promoted"
            session.flush()
        return
    if item.latest_job_id is None:
        return
    job = session.get(Job, item.latest_job_id)
    if job is None:
        raise RuntimeError("Intake extraction job is missing")
    latest_run = session.scalar(
        select(ExtractionRun).where(ExtractionRun.job_id == job.id)
        .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc()).limit(1)
    )
    if latest_run is not None:
        item.latest_run_id = latest_run.id
    if job.status is JobStatus.QUEUED:
        next_status = "queued"
    elif job.status is JobStatus.RUNNING:
        next_status = "running"
    elif job.status is JobStatus.FAILED:
        next_status = "failed"
    elif latest_run is not None and latest_run.status is ExtractionRunStatus.SUCCEEDED:
        next_status = "review_required"
        if not item.human_edited and item.draft_source_run_id != latest_run.id:
            try:
                result = ProductExtractionResult.model_validate(latest_run.structured_result)
                item.draft = _draft_from_observation(result).model_dump(mode="json")
            except ValidationError as exc:
                raise RuntimeError("Stored extraction result is invalid") from exc
            item.draft_source_run_id = latest_run.id
    else:
        next_status = "failed"
    if item.status != next_status:
        item.status = next_status
        item.updated_at = utc_now()
    session.flush()


def intake_read(session: Session, item: ProductIntakeItem) -> ProductIntakeRead:
    _refresh_status(session, item)
    draft = ProductIntakeDraft.model_validate(item.draft)
    job = session.get(Job, item.latest_job_id) if item.latest_job_id else None
    run = session.get(ExtractionRun, item.latest_run_id) if item.latest_run_id else None
    links = sorted(item.photos, key=lambda link: link.position)
    return ProductIntakeRead(
        id=item.id, status=item.status, created_at=item.created_at, updated_at=item.updated_at,
        draft=draft, human_edited=item.human_edited,
        photos=[IntakePhotoRead(
            id=link.photo_id, position=link.position, is_primary=link.is_primary,
            original_filename=link.photo.original_filename or "Photo",
            mime_type=link.photo.mime_type or "application/octet-stream",
            image_url=f"/api/product-intake/items/{item.id}/photos/{link.photo_id}/image",
        ) for link in links],
        extraction=IntakeExtractionRead(
            job_id=job.id if job else None, job_status=job.status.value if job else None,
            attempts=job.attempts if job else None, max_attempts=job.max_attempts if job else None,
            run_id=run.id if run else None, run_status=run.status.value if run else None,
            error=(run.sanitized_error if run and run.status is ExtractionRunStatus.FAILED else None)
            or ("Extraction failed. Check worker logs and retry with a new action." if job and job.status is JobStatus.FAILED else None),
            newer_result_available=bool(run and run.status is ExtractionRunStatus.SUCCEEDED and item.human_edited and item.draft_source_run_id != run.id),
            observation=ProductExtractionResult.model_validate(run.structured_result) if run and run.status is ExtractionRunStatus.SUCCEEDED else None,
        ),
        promotion=IntakePromotionLink(product_id=item.promotion.product_id, promoted_at=item.promotion.promoted_at) if item.promotion else None,
    )


def resolve_intake_photo(session: Session, item: ProductIntakeItem, photo_id: uuid.UUID, originals_dir: Path) -> tuple[Path, str]:
    link = next((link for link in item.photos if link.photo_id == photo_id), None)
    if link is None:
        raise UnknownIntakeError("Photo not found under this intake item.")
    photo = link.photo
    allowed_product_id = item.promotion.product_id if item.promotion else None
    if photo.product_id != allowed_product_id or photo.sku_id is not None or not photo.is_original:
        raise IntakeConflictError("Source photo ownership is invalid for intake.")
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(photo.mime_type)
    if extension is None:
        raise IntakeAssetError("Source photo is unavailable.")
    expected = originals_dir.resolve() / photo.checksum_sha256[:2] / f"{photo.checksum_sha256}{extension}"
    path = Path(photo.file_path).resolve()
    if path != expected or not path.is_file():
        raise IntakeAssetError("Source photo is unavailable.")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != photo.checksum_sha256:
        raise IntakeAssetError("Source photo failed integrity verification.")
    try:
        mime, _, width, height = inspect_supported_image(content)
    except ValueError as exc:
        raise IntakeAssetError("Source photo failed image verification.") from exc
    if (mime, width, height) != (photo.mime_type, photo.width, photo.height):
        raise IntakeAssetError("Source photo failed metadata verification.")
    return path, photo.mime_type


def _require_not_promoted(item: ProductIntakeItem) -> None:
    if item.promotion is not None or item.status == "promoted":
        raise IntakeConflictError("Promoted intake is read-only.")
