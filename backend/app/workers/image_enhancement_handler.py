import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.image_enhancement import (
    ImageEnhancementProvider,
    ImageEnhancementRequest,
    ImageEnhancementSource,
    PermanentImageEnhancementProviderError,
    RetryableImageEnhancementProviderError,
)
from app.ai.prompts.product_image_enhancement_v1 import (
    PRODUCT_IMAGE_ENHANCEMENT_PROMPT,
    PROMPT_VERSION,
)
from app.db.models import ImageEnhancementRun, Photo
from app.domain.schemas import ImageEnhancementJobPayload
from app.services.image_enhancement import (
    DEFAULT_PROCESSED_DIR,
    IMAGE_ENHANCEMENT_CONFIG_VERSION,
    IMAGE_ENHANCEMENT_JOB_TYPE,
    DerivedImageStorageError,
    ImageEnhancementRunError,
    complete_image_enhancement_run,
    create_running_image_enhancement_run,
    mark_image_enhancement_run_failed,
    store_processed_image,
)
from app.services.jobs import PermanentJobError
from app.services.image_processing import resolve_photo_for_processing, PhotoIntakeError, PhotoStorageIntegrityError
from app.workers.job_worker import ClaimedJob

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SessionFactory = Callable[[], Session]


class InvalidImageEnhancementHandlerJob(PermanentJobError):
    pass


class LocalImageEnhancementAssetError(PermanentJobError):
    pass


@dataclass(frozen=True)
class SourcePhotoMetadata:
    file_path: str = field(repr=False)
    checksum_sha256: str
    mime_type: str | None
    file_size_bytes: int | None
    width: int | None
    height: int | None
    is_original: bool = True


class ImageEnhancementJobHandler:
    def __init__(
        self,
        session_factory: SessionFactory,
        provider: ImageEnhancementProvider,
        *,
        processed_dir: Path = DEFAULT_PROCESSED_DIR,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._processed_dir = processed_dir

    def __call__(self, claimed: ClaimedJob) -> None:
        payload = self._validate_payload(claimed)
        run_id, source_metadata = self._prepare_attempt(claimed, payload)
        try:
            source = _load_verified_source(source_metadata)
            provider_result = self._provider.enhance(
                ImageEnhancementRequest(
                    source=source,
                    model=payload.model,
                    prompt=PRODUCT_IMAGE_ENHANCEMENT_PROMPT,
                    parameters=payload.parameters,
                )
            )
            stored_image = store_processed_image(
                provider_result.output_bytes,
                processed_dir=self._processed_dir,
            )
        except Exception as error:
            safe_error = _safe_attempt_error(error)
            self._fail_attempt(run_id, safe_error)
            raise safe_error from None

        try:
            with self._session_factory() as session:
                run = _require_run(session, run_id)
                complete_image_enhancement_run(
                    session,
                    run,
                    stored_image=stored_image,
                    usage=provider_result.usage,
                )
                session.commit()
        except Exception:
            error = RetryableImageEnhancementProviderError(
                "image enhancement database completion failed"
            )
            self._fail_attempt(run_id, error)
            raise error from None

    def _validate_payload(self, claimed: ClaimedJob) -> ImageEnhancementJobPayload:
        if claimed.job_type != IMAGE_ENHANCEMENT_JOB_TYPE:
            raise InvalidImageEnhancementHandlerJob(
                f"handler requires job type {IMAGE_ENHANCEMENT_JOB_TYPE}"
            )
        try:
            payload = ImageEnhancementJobPayload.model_validate(claimed.payload)
        except ValidationError:
            raise InvalidImageEnhancementHandlerJob(
                "invalid image enhancement job payload"
            ) from None
        if payload.provider != self._provider.name:
            raise InvalidImageEnhancementHandlerJob(
                "job provider does not match configured image provider"
            )
        if payload.prompt_version != PROMPT_VERSION:
            raise InvalidImageEnhancementHandlerJob(
                f"unsupported image enhancement prompt: {payload.prompt_version}"
            )
        if payload.config_version != IMAGE_ENHANCEMENT_CONFIG_VERSION:
            raise InvalidImageEnhancementHandlerJob(
                f"unsupported image enhancement config: {payload.config_version}"
            )
        return payload

    def _prepare_attempt(
        self,
        claimed: ClaimedJob,
        payload: ImageEnhancementJobPayload,
    ) -> tuple[uuid.UUID, SourcePhotoMetadata]:
        with self._session_factory() as session:
            try:
                run = create_running_image_enhancement_run(
                    session,
                    payload=payload,
                    job_id=claimed.id,
                )
            except ImageEnhancementRunError as error:
                raise InvalidImageEnhancementHandlerJob(str(error)) from None
            metadata = SourcePhotoMetadata(
                file_path=run.source_photo.file_path,
                checksum_sha256=run.source_photo.checksum_sha256.lower(),
                mime_type=run.source_photo.mime_type,
                file_size_bytes=run.source_photo.file_size_bytes,
                width=run.source_photo.width,
                height=run.source_photo.height,
            )
            session.commit()
            return run.id, metadata

    def _fail_attempt(self, run_id: uuid.UUID, error: Exception) -> None:
        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_image_enhancement_run_failed(session, run, error=error)
            session.commit()


def image_enhancement_handlers(
    session_factory: SessionFactory,
    provider: ImageEnhancementProvider,
    *,
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> dict[str, ImageEnhancementJobHandler]:
    return {
        IMAGE_ENHANCEMENT_JOB_TYPE: ImageEnhancementJobHandler(
            session_factory,
            provider,
            processed_dir=processed_dir,
        )
    }


def _load_verified_source(metadata: SourcePhotoMetadata) -> ImageEnhancementSource:
    try:
        asset = resolve_photo_for_processing(metadata)
        content = asset.file_path.read_bytes()
    except (OSError, PhotoIntakeError, PhotoStorageIntegrityError):
        raise LocalImageEnhancementAssetError(
            "referenced original Photo is unavailable, corrupt or unsupported"
        ) from None
    return ImageEnhancementSource(
        content=content,
        mime_type=asset.mime_type,
        checksum_sha256=asset.checksum_sha256,
    )


def _safe_attempt_error(error: Exception) -> Exception:
    if isinstance(
        error,
        (
            PermanentImageEnhancementProviderError,
            RetryableImageEnhancementProviderError,
            PermanentJobError,
        ),
    ):
        return error
    if isinstance(error, DerivedImageStorageError):
        return PermanentImageEnhancementProviderError(
            "derived image validation or storage failed"
        )
    return RetryableImageEnhancementProviderError(
        "image enhancement provider attempt failed"
    )


def _require_run(session: Session, run_id: uuid.UUID) -> ImageEnhancementRun:
    run = session.get(ImageEnhancementRun, run_id)
    if run is None:
        raise RuntimeError("image enhancement run disappeared during execution")
    return run
