import hashlib
import os
import tempfile
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.db.models import Photo, SKU
from app.domain.enums import PhotoRole

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ORIGINALS_DIR = PROJECT_ROOT / "storage" / "originals"

SUPPORTED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class PhotoIntakeError(ValueError):
    pass


class UnsupportedPhotoFormatError(PhotoIntakeError):
    pass


class UnknownSKUError(PhotoIntakeError):
    pass


class PhotoStorageIntegrityError(RuntimeError):
    pass


def inspect_supported_image(image_bytes: bytes) -> tuple[str, str, int, int]:
    if not image_bytes:
        raise PhotoIntakeError("uploaded image is empty")

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            detected_format = image.format
            width, height = image.size
            image.verify()

        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise PhotoIntakeError("uploaded file is not a decodable image") from exc

    if detected_format not in SUPPORTED_FORMATS:
        raise UnsupportedPhotoFormatError(
            f"unsupported image format: {detected_format or 'unknown'}"
        )
    if width <= 0 or height <= 0:
        raise PhotoIntakeError("uploaded image has invalid dimensions")

    mime_type, extension = SUPPORTED_FORMATS[detected_format]
    return mime_type, extension, width, height


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
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        stored_checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
        if stored_checksum != checksum_sha256:
            raise PhotoStorageIntegrityError(
                f"stored original does not match its checksum identity: {destination}"
            )
        return destination

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".photo-upload-",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(image_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)

    return destination


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

    mime_type, extension, width, height = inspect_supported_image(image_bytes)
    checksum_sha256 = hashlib.sha256(image_bytes).hexdigest()
    stored_path = _store_original_bytes(
        image_bytes,
        checksum_sha256=checksum_sha256,
        extension=extension,
        originals_dir=originals_dir,
    )

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
