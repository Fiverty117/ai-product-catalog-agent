import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.ai.image_enhancement import (
    OPENAI_IMAGE_PROVIDER,
    configured_openai_image_model,
    normalize_image_enhancement_parameters,
)
from app.ai.prompts.product_image_enhancement_v1 import PROMPT_VERSION
from app.db.models import DerivedImage, ImageEnhancementRun, Job, Photo
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus
from app.domain.schemas import ImageEnhancementJobPayload
from app.services.extraction import sanitize_extraction_error
from app.services.jobs import enqueue_job
from app.services.photo_intake import (
    DEFAULT_ORIGINALS_DIR,
    PhotoIntakeError,
    inspect_supported_image,
)

IMAGE_ENHANCEMENT_JOB_TYPE = "image.enhance.v1"
IMAGE_ENHANCEMENT_CONFIG_VERSION = "image-enhancement-config-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROCESSED_DIR = PROJECT_ROOT / "storage" / "processed"


class ImageEnhancementRunError(ValueError):
    pass


class UnknownImageEnhancementPhotoError(ImageEnhancementRunError):
    pass


class IneligibleImageEnhancementPhotoError(ImageEnhancementRunError):
    pass


class InvalidImageEnhancementJobError(ImageEnhancementRunError):
    pass


class CompletedImageEnhancementRunError(ImageEnhancementRunError):
    pass


class DerivedImageStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredDerivedImage:
    file_path: str
    checksum_sha256: str
    mime_type: str
    file_size_bytes: int
    width: int
    height: int


def build_image_enhancement_idempotency_key(
    payload: ImageEnhancementJobPayload,
) -> str:
    identity = {
        "job_type": IMAGE_ENHANCEMENT_JOB_TYPE,
        "source_photo_id": str(payload.source_photo_id),
        "source_checksum_sha256": payload.source_checksum_sha256.lower(),
        "provider": payload.provider,
        "model": payload.model,
        "prompt_version": payload.prompt_version,
        "config_version": payload.config_version,
        "parameters": payload.parameters,
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"{IMAGE_ENHANCEMENT_JOB_TYPE}:{digest}"


def hash_image_enhancement_parameters(parameters: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(parameters).encode("utf-8")).hexdigest()


def enqueue_image_enhancement(
    session: Session,
    *,
    source_photo_id: uuid.UUID,
    provider: str = OPENAI_IMAGE_PROVIDER,
    model: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    config_version: str = IMAGE_ENHANCEMENT_CONFIG_VERSION,
    parameters: Mapping[str, object] | None = None,
    max_attempts: int = 3,
) -> Job:
    photo = session.get(Photo, source_photo_id)
    if photo is None:
        raise UnknownImageEnhancementPhotoError(
            f"Photo not found: {source_photo_id}"
        )
    if not photo.is_original:
        raise IneligibleImageEnhancementPhotoError(
            "image enhancement source must be an original Photo"
        )
    normalized_parameters = normalize_image_enhancement_parameters(parameters)
    payload = ImageEnhancementJobPayload(
        source_photo_id=photo.id,
        source_checksum_sha256=photo.checksum_sha256.lower(),
        provider=provider,
        model=model or configured_openai_image_model(),
        prompt_version=prompt_version,
        config_version=config_version,
        parameters=normalized_parameters,
    )
    return enqueue_job(
        session,
        job_type=IMAGE_ENHANCEMENT_JOB_TYPE,
        payload=payload.model_dump(mode="json"),
        idempotency_key=build_image_enhancement_idempotency_key(payload),
        max_attempts=max_attempts,
    )


def create_running_image_enhancement_run(
    session: Session,
    *,
    payload: ImageEnhancementJobPayload,
    job_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
) -> ImageEnhancementRun:
    photo = session.get(Photo, payload.source_photo_id)
    if photo is None:
        raise UnknownImageEnhancementPhotoError(
            f"Photo not found: {payload.source_photo_id}"
        )
    if not photo.is_original:
        raise IneligibleImageEnhancementPhotoError(
            "image enhancement source must be an original Photo"
        )
    if photo.checksum_sha256.lower() != payload.source_checksum_sha256.lower():
        raise InvalidImageEnhancementJobError(
            "job source checksum does not match the Photo record"
        )
    job = None
    if job_id is not None:
        job = session.get(Job, job_id)
        if job is None or job.job_type != IMAGE_ENHANCEMENT_JOB_TYPE:
            raise InvalidImageEnhancementJobError(
                f"job must exist with type {IMAGE_ENHANCEMENT_JOB_TYPE}"
            )
    run = ImageEnhancementRun(
        source_photo=photo,
        job=job,
        provider=payload.provider,
        model=payload.model,
        prompt_version=payload.prompt_version,
        config_version=payload.config_version,
        parameters_hash=hash_image_enhancement_parameters(payload.parameters),
        status=ExtractionRunStatus.RUNNING,
        started_at=started_at or utc_now(),
    )
    session.add(run)
    session.flush()
    return run


def complete_image_enhancement_run(
    session: Session,
    run: ImageEnhancementRun,
    *,
    stored_image: StoredDerivedImage,
    usage: Mapping[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> DerivedImage:
    _require_running(run)
    derived = DerivedImage(
        source_photo_id=run.source_photo_id,
        enhancement_run=run,
        file_path=stored_image.file_path,
        checksum_sha256=stored_image.checksum_sha256,
        mime_type=stored_image.mime_type,
        file_size_bytes=stored_image.file_size_bytes,
        width=stored_image.width,
        height=stored_image.height,
    )
    session.add(derived)
    run.status = ExtractionRunStatus.SUCCEEDED
    run.usage = dict(usage) if usage is not None else None
    run.sanitized_error = None
    run.completed_at = completed_at or utc_now()
    session.flush()
    return derived


def mark_image_enhancement_run_failed(
    session: Session,
    run: ImageEnhancementRun,
    *,
    error: Exception | str,
    completed_at: datetime | None = None,
) -> ImageEnhancementRun:
    _require_running(run)
    run.status = ExtractionRunStatus.FAILED
    run.usage = None
    run.sanitized_error = sanitize_extraction_error(error)
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def store_processed_image(
    output_bytes: bytes,
    *,
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> StoredDerivedImage:
    try:
        mime_type, extension, width, height = inspect_supported_image(output_bytes)
    except PhotoIntakeError:
        raise DerivedImageStorageError(
            "provider output is corrupt or uses an unsupported image format"
        ) from None
    checksum = hashlib.sha256(output_bytes).hexdigest()
    processed_root = processed_dir.resolve()
    originals_root = DEFAULT_ORIGINALS_DIR.resolve()
    if processed_root == originals_root or originals_root in processed_root.parents:
        raise DerivedImageStorageError("processed output cannot be stored in originals")
    destination = processed_root / checksum[:2] / f"{checksum}{extension}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            existing = destination.read_bytes()
        except OSError:
            raise DerivedImageStorageError(
                "existing processed image could not be verified"
            ) from None
        if hashlib.sha256(existing).hexdigest() != checksum:
            raise DerivedImageStorageError(
                "existing processed image does not match its content identity"
            )
    else:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=".image-enhancement-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(output_bytes)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
        except OSError:
            raise DerivedImageStorageError(
                "processed image could not be stored"
            ) from None
        finally:
            temporary_path.unlink(missing_ok=True)
    return StoredDerivedImage(
        file_path=str(destination),
        checksum_sha256=checksum,
        mime_type=mime_type,
        file_size_bytes=len(output_bytes),
        width=width,
        height=height,
    )


def _require_running(run: ImageEnhancementRun) -> None:
    if run.status is not ExtractionRunStatus.RUNNING:
        raise CompletedImageEnhancementRunError(
            f"image enhancement run is already complete: {run.id}"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
