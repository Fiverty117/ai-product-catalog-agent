import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DerivedImage,
    DerivedImageReview,
    ImageEnhancementRun,
    Photo,
    PhotoPresentationPreference,
)
from app.domain.enums import (
    DerivedImageReviewDecision,
    DerivedImageReviewState,
    ExtractionRunStatus,
    PhotoPresentationAssetType,
    PhotoPresentationWarning,
)
from app.domain.schemas import (
    DerivedImageReviewCreate,
    EffectivePhotoPresentation,
    PhotoPresentationSelection,
)
from app.services.photo_intake import PhotoIntakeError, inspect_supported_image
from app.services.image_processing import resolve_photo_for_processing, PhotoStorageIntegrityError

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ImagePresentationError(ValueError):
    pass


class UnknownDerivedImageError(ImagePresentationError):
    pass


class UnknownPresentationPhotoError(ImagePresentationError):
    pass


class InvalidDerivedImageLineageError(ImagePresentationError):
    pass


class DerivedImageAssetIntegrityError(ImagePresentationError):
    pass


class UnapprovedDerivedImageError(ImagePresentationError):
    pass


def create_derived_image_review(
    session: Session,
    request: DerivedImageReviewCreate | Mapping[str, Any],
) -> DerivedImageReview:
    """Append one human review and safely clear a rejected active selection."""

    validated = DerivedImageReviewCreate.model_validate(request)
    derived = _require_reviewable_derived_image(
        session, validated.derived_image_id
    )
    preference = None
    if validated.decision is DerivedImageReviewDecision.REJECTED:
        preference = session.scalar(
            select(PhotoPresentationPreference).where(
                PhotoPresentationPreference.photo_id == derived.source_photo_id
            )
        )
    review = DerivedImageReview(
        derived_image=derived,
        decision=validated.decision,
    )
    session.add(review)

    if (
        preference is not None
        and preference.selected_derived_image_id == derived.id
    ):
        preference.selected_derived_image = None

    session.flush()
    return review


def get_current_derived_image_review(
    session: Session,
    derived_image_id: uuid.UUID,
) -> DerivedImageReview | None:
    _require_derived_image(session, derived_image_id)
    return session.scalar(
        select(DerivedImageReview)
        .where(DerivedImageReview.derived_image_id == derived_image_id)
        .order_by(
            DerivedImageReview.created_at.desc(),
            DerivedImageReview.id.desc(),
        )
        .limit(1)
    )


def get_derived_image_review_state(
    session: Session,
    derived_image_id: uuid.UUID,
) -> DerivedImageReviewState:
    review = get_current_derived_image_review(session, derived_image_id)
    if review is None:
        return DerivedImageReviewState.UNREVIEWED
    return DerivedImageReviewState(review.decision.value)


def is_derived_image_approved(
    session: Session,
    derived_image_id: uuid.UUID,
) -> bool:
    return (
        get_derived_image_review_state(session, derived_image_id)
        is DerivedImageReviewState.APPROVED
    )


def select_derived_image_for_photo(
    session: Session,
    *,
    photo_id: uuid.UUID,
    derived_image_id: uuid.UUID,
) -> PhotoPresentationPreference:
    request = PhotoPresentationSelection(
        photo_id=photo_id,
        derived_image_id=derived_image_id,
    )
    photo = _require_photo(session, request.photo_id)
    derived = _require_derived_image(session, request.derived_image_id)
    if derived.source_photo_id != photo.id:
        raise InvalidDerivedImageLineageError(
            "DerivedImage does not belong to the selected source Photo"
        )
    if not is_derived_image_approved(session, derived.id):
        raise UnapprovedDerivedImageError(
            "only a currently approved DerivedImage may be selected"
        )
    _require_reviewable_derived_image(session, derived.id)

    preference = _get_photo_preference(session, photo.id)
    if preference is None:
        preference = PhotoPresentationPreference(
            photo=photo,
            selected_derived_image=derived,
        )
        session.add(preference)
    elif preference.selected_derived_image_id != derived.id:
        preference.selected_derived_image = derived
    session.flush()
    return preference


def use_original_photo_presentation(
    session: Session,
    *,
    photo_id: uuid.UUID,
) -> PhotoPresentationPreference:
    photo = _require_photo(session, photo_id)
    preference = _get_photo_preference(session, photo.id)
    if preference is None:
        preference = PhotoPresentationPreference(photo=photo)
        session.add(preference)
    elif preference.selected_derived_image_id is not None:
        preference.selected_derived_image = None
    session.flush()
    return preference


def resolve_effective_photo_presentation(
    session: Session,
    *,
    photo_id: uuid.UUID,
) -> EffectivePhotoPresentation:
    """Resolve current presentation without repairing or mutating preference."""

    photo = _require_photo(session, photo_id)
    preference = _get_photo_preference(session, photo.id)
    if preference is None or preference.selected_derived_image_id is None:
        return _original_presentation(photo)

    derived = session.get(DerivedImage, preference.selected_derived_image_id)
    if (
        derived is None
        or derived.source_photo_id != photo.id
        or get_derived_image_review_state(session, derived.id)
        is not DerivedImageReviewState.APPROVED
    ):
        return _original_presentation(
            photo,
            warnings=[
                PhotoPresentationWarning.PREFERRED_DERIVED_SELECTION_INVALID
            ],
        )

    if not _asset_path(derived.file_path).is_file():
        return _original_presentation(
            photo,
            warnings=[
                PhotoPresentationWarning.PREFERRED_DERIVED_ASSET_MISSING
            ],
        )

    return EffectivePhotoPresentation(
        source_photo_id=photo.id,
        asset_type=PhotoPresentationAssetType.DERIVED,
        derived_image_id=derived.id,
        file_path=derived.file_path,
        checksum_sha256=derived.checksum_sha256,
        mime_type=derived.mime_type,
        width=derived.width,
        height=derived.height,
        backing_asset_available=True,
        warnings=[],
    )


def _require_reviewable_derived_image(
    session: Session,
    derived_image_id: uuid.UUID,
) -> DerivedImage:
    derived = _require_derived_image(session, derived_image_id)
    if session.get(Photo, derived.source_photo_id) is None:
        raise InvalidDerivedImageLineageError(
            "DerivedImage source Photo no longer exists"
        )
    run = session.get(ImageEnhancementRun, derived.enhancement_run_id)
    if run is None or run.status is not ExtractionRunStatus.SUCCEEDED:
        raise InvalidDerivedImageLineageError(
            "DerivedImage requires a succeeded ImageEnhancementRun"
        )
    if run.source_photo_id != derived.source_photo_id:
        raise InvalidDerivedImageLineageError(
            "DerivedImage source does not match its ImageEnhancementRun"
        )
    _validate_derived_asset(derived)
    return derived


def _validate_derived_asset(derived: DerivedImage) -> None:
    path = _asset_path(derived.file_path)
    try:
        content = path.read_bytes()
    except OSError:
        raise DerivedImageAssetIntegrityError(
            "DerivedImage backing asset is unavailable"
        ) from None
    if hashlib.sha256(content).hexdigest() != derived.checksum_sha256.lower():
        raise DerivedImageAssetIntegrityError(
            "DerivedImage backing asset failed checksum verification"
        )
    try:
        mime_type, _, width, height = inspect_supported_image(content)
    except PhotoIntakeError:
        raise DerivedImageAssetIntegrityError(
            "DerivedImage backing asset is corrupt or unsupported"
        ) from None
    if (
        mime_type != derived.mime_type
        or len(content) != derived.file_size_bytes
        or width != derived.width
        or height != derived.height
    ):
        raise DerivedImageAssetIntegrityError(
            "DerivedImage backing asset metadata does not match persisted metadata"
        )


def _require_derived_image(
    session: Session,
    derived_image_id: uuid.UUID,
) -> DerivedImage:
    derived = session.get(DerivedImage, derived_image_id)
    if derived is None:
        raise UnknownDerivedImageError(
            f"DerivedImage not found: {derived_image_id}"
        )
    return derived


def _require_photo(session: Session, photo_id: uuid.UUID) -> Photo:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise UnknownPresentationPhotoError(f"Photo not found: {photo_id}")
    return photo


def _get_photo_preference(
    session: Session,
    photo_id: uuid.UUID,
) -> PhotoPresentationPreference | None:
    return session.scalar(
        select(PhotoPresentationPreference).where(
            PhotoPresentationPreference.photo_id == photo_id
        )
    )


def _original_presentation(
    photo: Photo,
    *,
    warnings: list[PhotoPresentationWarning] | None = None,
) -> EffectivePhotoPresentation:
    try:
        asset = resolve_photo_for_processing(photo)
    except (OSError, PhotoIntakeError, PhotoStorageIntegrityError):
        asset = None
    return EffectivePhotoPresentation(
        source_photo_id=photo.id,
        asset_type=PhotoPresentationAssetType.ORIGINAL,
        derived_image_id=None,
        file_path=str(asset.file_path) if asset else photo.file_path,
        checksum_sha256=asset.checksum_sha256 if asset else photo.checksum_sha256,
        mime_type=asset.mime_type if asset else photo.mime_type,
        width=asset.width if asset else photo.width,
        height=asset.height if asset else photo.height,
        backing_asset_available=asset is not None,
        warnings=warnings or [],
    )


def _asset_path(file_path: str) -> Path:
    path = Path(file_path)
    return path if path.is_absolute() else PROJECT_ROOT / path
