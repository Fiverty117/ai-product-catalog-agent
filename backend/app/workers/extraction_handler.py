import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.prompts.product_extraction_v1 import (
    PRODUCT_EXTRACTION_PROMPT,
    PROMPT_VERSION,
)
from app.ai.vision import (
    PermanentVisionProviderError,
    RetryableVisionProviderError,
    VisionExtractionRequest,
    VisionImage,
    VisionProvider,
)
from app.db.models import ExtractionRun, Photo
from app.domain.schemas import ProductExtractionJobPayload, ProductExtractionResult
from app.services.extraction import (
    PRODUCT_EXTRACTION_JOB_TYPE,
    PRODUCT_EXTRACTION_SCHEMA_VERSION,
    create_running_extraction_run,
    mark_extraction_run_failed,
    mark_extraction_run_succeeded,
)
from app.services.jobs import PermanentJobError
from app.services.image_processing import resolve_photo_for_processing, PhotoIntakeError, PhotoStorageIntegrityError
from app.workers.job_worker import ClaimedJob

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SessionFactory = Callable[[], Session]


class InvalidProductExtractionJobError(PermanentJobError):
    pass


class LocalPhotoAssetError(PermanentJobError):
    pass


class ProductExtractionJobHandler:
    def __init__(
        self,
        session_factory: SessionFactory,
        provider: VisionProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    def __call__(self, claimed: ClaimedJob) -> None:
        payload = self._validate_payload(claimed)
        run_id, images = self._prepare_attempt(claimed, payload)
        request = VisionExtractionRequest(
            images=images,
            model=payload.model,
            prompt=PRODUCT_EXTRACTION_PROMPT,
            parameters=payload.parameters,
        )

        try:
            provider_result = self._provider.extract(request)
            structured_result = ProductExtractionResult.model_validate(
                provider_result.structured_result
            )
        except ValidationError:
            error = RetryableVisionProviderError(
                "vision provider returned structured output that failed validation"
            )
            self._fail_attempt(run_id, error)
            raise error from None
        except Exception as error:
            safe_error = _safe_provider_error(error)
            self._fail_attempt(run_id, safe_error)
            raise safe_error from None

        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_extraction_run_succeeded(
                session,
                run,
                structured_result=structured_result,
                usage=provider_result.usage,
            )
            session.commit()

    def _validate_payload(self, claimed: ClaimedJob) -> ProductExtractionJobPayload:
        if claimed.job_type != PRODUCT_EXTRACTION_JOB_TYPE:
            raise InvalidProductExtractionJobError(
                f"handler requires job type {PRODUCT_EXTRACTION_JOB_TYPE}"
            )
        try:
            payload = ProductExtractionJobPayload.model_validate(claimed.payload)
        except ValidationError:
            raise InvalidProductExtractionJobError(
                "invalid product extraction job payload"
            ) from None
        if payload.provider != self._provider.name:
            raise InvalidProductExtractionJobError(
                "job payload provider does not match the configured vision provider"
            )
        if payload.prompt_version != PROMPT_VERSION:
            raise InvalidProductExtractionJobError(
                f"unsupported extraction prompt version: {payload.prompt_version}"
            )
        if payload.schema_version != PRODUCT_EXTRACTION_SCHEMA_VERSION:
            raise InvalidProductExtractionJobError(
                f"unsupported extraction schema version: {payload.schema_version}"
            )
        return payload

    def _prepare_attempt(
        self,
        claimed: ClaimedJob,
        payload: ProductExtractionJobPayload,
    ) -> tuple[uuid.UUID, tuple[VisionImage, ...]]:
        with self._session_factory() as session:
            photos = session.scalars(
                select(Photo).where(Photo.id.in_(payload.photo_ids))
            ).all()
            photos_by_id = {photo.id: photo for photo in photos}
            if set(photos_by_id) != set(payload.photo_ids):
                raise LocalPhotoAssetError("one or more referenced photos do not exist")

            ordered_photos = sorted(
                photos,
                key=lambda photo: (photo.checksum_sha256.lower(), str(photo.id)),
            )
            images = tuple(_load_verified_original(photo) for photo in ordered_photos)
            canonical_payload = payload.model_copy(
                update={"photo_ids": [photo.id for photo in ordered_photos]}
            )
            sku_id = _shared_sku_id(ordered_photos)
            run = create_running_extraction_run(
                session,
                payload=canonical_payload,
                job_id=claimed.id,
                sku_id=sku_id,
            )
            session.commit()
            return run.id, images

    def _fail_attempt(self, run_id: uuid.UUID, error: Exception) -> None:
        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_extraction_run_failed(session, run, error=error)
            session.commit()


def product_extraction_handlers(
    session_factory: SessionFactory,
    provider: VisionProvider,
) -> dict[str, ProductExtractionJobHandler]:
    return {
        PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(
            session_factory,
            provider,
        )
    }


def _load_verified_original(photo: Photo) -> VisionImage:
    if not photo.mime_type:
        raise LocalPhotoAssetError("referenced original photo has no MIME type")
    try:
        asset = resolve_photo_for_processing(photo)
        content = asset.file_path.read_bytes()
    except (OSError, PhotoIntakeError, PhotoStorageIntegrityError):
        raise LocalPhotoAssetError("referenced original photo is unavailable or failed verification") from None
    return VisionImage(
        content=content,
        mime_type=asset.mime_type,
        checksum_sha256=asset.checksum_sha256,
    )


def _shared_sku_id(photos: list[Photo]) -> uuid.UUID | None:
    sku_ids = {photo.sku_id for photo in photos}
    if len(sku_ids) == 1:
        return next(iter(sku_ids))
    return None


def _safe_provider_error(error: Exception) -> Exception:
    if isinstance(error, (PermanentVisionProviderError, RetryableVisionProviderError)):
        return error
    if isinstance(error, PermanentJobError):
        return error
    return RetryableVisionProviderError("vision provider attempt failed")


def _require_run(session: Session, run_id: uuid.UUID) -> ExtractionRun:
    run = session.get(ExtractionRun, run_id)
    if run is None:
        raise RuntimeError("extraction run disappeared during execution")
    return run
