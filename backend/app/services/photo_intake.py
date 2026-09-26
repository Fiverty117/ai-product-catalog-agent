import hashlib
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Photo, SKU
from app.domain.enums import PhotoRole
from app.services.image_processing import (
    PhotoIntakeError, UnsupportedPhotoFormatError, PhotoStorageIntegrityError,
    SUPPORTED_FORMATS, inspect_supported_image, inspect_original_image,
    store_immutable_bytes, ensure_processing_representation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ORIGINALS_DIR = PROJECT_ROOT / "storage" / "originals"

class UnknownSKUError(PhotoIntakeError):
    pass


def _store_original_bytes(
    image_bytes: bytes,
    *,
    checksum_sha256: str,
    extension: str,
    originals_dir: Path,
) -> Path:
    destination = (
        originals_dir.resolve()
        / checksum_sha256[:2]
        / f"{checksum_sha256}{extension}"
    )
    if not destination.resolve().is_relative_to(originals_dir.resolve()):
        raise PhotoStorageIntegrityError("original asset path is outside storage")
    return store_immutable_bytes(image_bytes, destination, checksum_sha256)


def register_original_photo(
    session: Session,
    *,
    image_bytes: bytes,
    original_filename: str,
    originals_dir: Path,
    sku_id: uuid.UUID | None = None,
    role: PhotoRole = PhotoRole.OTHER,
) -> Photo:
    """Validate, preserve, and register one user-uploaded original photo."""

    if not original_filename.strip():
        raise PhotoIntakeError("original filename is required")
    if len(original_filename) > 255:
        raise PhotoIntakeError("original filename exceeds 255 characters")

    sku = None
    if sku_id is not None:
        sku = session.get(SKU, sku_id)
        if sku is None:
            raise UnknownSKUError(f"SKU not found: {sku_id}")

    mime_type, extension, width, height = inspect_original_image(image_bytes)
    checksum_sha256 = hashlib.sha256(image_bytes).hexdigest()
    stored_path = _store_original_bytes(
        image_bytes,
        checksum_sha256=checksum_sha256,
        extension=extension,
        originals_dir=originals_dir,
    )
    ensure_processing_representation(image_bytes, stored_path, checksum_sha256)

    photo = Photo(
        sku=sku,
        file_path=str(stored_path),
        checksum_sha256=checksum_sha256,
        original_filename=original_filename,
        mime_type=mime_type,
        file_size_bytes=len(image_bytes),
        width=width,
        height=height,
        role=role,
        is_original=True,
    )
    session.add(photo)
    session.flush()
    return photo
