import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExtractionRun, Job, Photo, SKU
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus
from app.domain.schemas import ProductExtractionJobPayload, ProductExtractionResult

PRODUCT_EXTRACTION_JOB_TYPE = "product.extract.v1"
PRODUCT_EXTRACTION_SCHEMA_VERSION = "product-result-v1"


class ExtractionRunError(ValueError):
    pass


class UnknownExtractionPhotoError(ExtractionRunError):
    pass


class UnknownExtractionSKUError(ExtractionRunError):
    pass


class InvalidExtractionJobError(ExtractionRunError):
    pass


class CompletedExtractionRunError(ExtractionRunError):
    pass


def build_product_extraction_idempotency_key(
    payload: ProductExtractionJobPayload,
    photo_checksums: Mapping[uuid.UUID, str],
) -> str:
    expected_photo_ids = set(payload.photo_ids)
    if set(photo_checksums) != expected_photo_ids:
        raise ValueError("photo_checksums must match payload photo_ids exactly")

    normalized_checksums = []
    for photo_id in payload.photo_ids:
        checksum = photo_checksums[photo_id].lower()
        if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise ValueError(f"invalid SHA-256 checksum for photo: {photo_id}")
        normalized_checksums.append(checksum)

    identity = {
        "job_type": PRODUCT_EXTRACTION_JOB_TYPE,
        "photo_checksums": sorted(normalized_checksums),
        "provider": payload.provider,
        "model": payload.model,
        "prompt_version": payload.prompt_version,
        "schema_version": payload.schema_version,
        "parameters": payload.parameters,
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"{PRODUCT_EXTRACTION_JOB_TYPE}:{digest}"


def hash_extraction_parameters(parameters: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(parameters).encode("utf-8")).hexdigest()


def create_running_extraction_run(
    session: Session,
    *,
    payload: ProductExtractionJobPayload,
    job_id: uuid.UUID | None = None,
    sku_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
) -> ExtractionRun:
    photos_by_id = {
        photo.id: photo
        for photo in session.scalars(
            select(Photo).where(Photo.id.in_(payload.photo_ids))
        ).all()
    }
    missing_photo_ids = [
        photo_id for photo_id in payload.photo_ids if photo_id not in photos_by_id
    ]
    if missing_photo_ids:
        raise UnknownExtractionPhotoError(
            "photos not found: " + ", ".join(str(photo_id) for photo_id in missing_photo_ids)
        )

    job = None
    if job_id is not None:
        job = session.get(Job, job_id)
        if job is None or job.job_type != PRODUCT_EXTRACTION_JOB_TYPE:
            raise InvalidExtractionJobError(
                f"job must exist with type {PRODUCT_EXTRACTION_JOB_TYPE}: {job_id}"
            )

    sku = None
    if sku_id is not None:
        sku = session.get(SKU, sku_id)
        if sku is None:
            raise UnknownExtractionSKUError(f"SKU not found: {sku_id}")

    run = ExtractionRun(
        job=job,
        sku=sku,
        provider=payload.provider,
        model=payload.model,
        prompt_version=payload.prompt_version,
        schema_version=payload.schema_version,
        parameters_hash=hash_extraction_parameters(payload.parameters),
        status=ExtractionRunStatus.RUNNING,
        started_at=started_at or utc_now(),
        photos=[photos_by_id[photo_id] for photo_id in payload.photo_ids],
    )
    session.add(run)
    session.flush()
    return run


def mark_extraction_run_succeeded(
    session: Session,
    run: ExtractionRun,
    *,
    structured_result: ProductExtractionResult | Mapping[str, Any],
    usage: Mapping[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> ExtractionRun:
    _require_running(run)
    validated_result = ProductExtractionResult.model_validate(structured_result)
    run.status = ExtractionRunStatus.SUCCEEDED
    run.structured_result = validated_result.model_dump(mode="json")
    run.usage = dict(usage) if usage is not None else None
    run.sanitized_error = None
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def mark_extraction_run_failed(
    session: Session,
    run: ExtractionRun,
    *,
    error: Exception | str,
    completed_at: datetime | None = None,
) -> ExtractionRun:
    _require_running(run)
    run.status = ExtractionRunStatus.FAILED
    run.structured_result = None
    run.usage = None
    run.sanitized_error = sanitize_extraction_error(error)
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def sanitize_extraction_error(error: Exception | str, limit: int = 1000) -> str:
    if isinstance(error, Exception):
        message = f"{type(error).__name__}: {error}"
    else:
        message = error
    message = " ".join(message.split())
    message = re.sub(
        r"(?i)\b(api[_-]?key|authorization|access[_-]?token|bearer[_-]?token|"
        r"password|secret)\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
        r"\1=[REDACTED]",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [REDACTED]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", message)
    return message[:limit]


def _require_running(run: ExtractionRun) -> None:
    if run.status is not ExtractionRunStatus.RUNNING:
        raise CompletedExtractionRunError(
            f"extraction run is already complete: {run.id} ({run.status.value})"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
