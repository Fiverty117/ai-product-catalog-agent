"""Original image validation and deterministic compatibility representations.

MPO is a source format, never a processing format. Frame 0 is Pillow's default
primary image. A versioned source index refers to a content-addressed JPEG;
neither file is a Photo or an AI DerivedImage.
"""

import hashlib
import json
import os
import re
import struct
import tempfile
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESSING_VERSION = "image-processing-v1"
SUPPORTED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
ORIGINAL_FORMATS = {**SUPPORTED_FORMATS, "MPO": ("image/mpo", ".mpo")}


class PhotoIntakeError(ValueError):
    pass


class UnsupportedPhotoFormatError(PhotoIntakeError):
    pass


class PhotoStorageIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessingImage:
    file_path: Path
    checksum_sha256: str
    mime_type: str
    file_size_bytes: int
    width: int
    height: int


def _inspect_image(content: bytes, *, allow_mpo: bool) -> tuple[str, str, int, int]:
    if not content:
        raise PhotoIntakeError("uploaded image is empty")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Pillow otherwise warns and falls back to JPEG for a malformed MP index.
            warnings.filterwarnings("error", message="Image appears to be a malformed MPO.*")
            with Image.open(BytesIO(content)) as image:
                detected_format = image.format
                if detected_format == "JPEG" and "mp" in image.info:
                    # jpeg_factory silently falls back on some malformed MP types.
                    # Validate the index even when Pillow did not promote it to MPO.
                    mp = image._getmp()
                    if not mp or not isinstance(mp[0xB001], int) or mp[0xB001] < 1 or len(mp[0xB002]) != mp[0xB001]:
                        raise ValueError("invalid MPO index")
                width, height = image.size
                image.verify()
            with Image.open(BytesIO(content)) as image:
                if detected_format == "MPO":
                    # Validate the container and each frame, without keeping decoded copies.
                    entries = image.mpinfo[0xB002]
                    for index, entry in enumerate(entries):
                        image.seek(index)
                        # Image.open checks frame 0; MPO.seek does not repeat that check.
                        if Image.MAX_IMAGE_PIXELS is not None and image.width * image.height > Image.MAX_IMAGE_PIXELS:
                            raise ValueError("MPO frame exceeds decoded pixel safety limit")
                        offset = image.offset
                        if entry["Size"] < 4 or offset < 0 or offset + entry["Size"] > len(content):
                            raise ValueError("invalid MPO frame bounds")
                        image.load()
                else:
                    image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning,
            UnidentifiedImageError, OSError, SyntaxError, ValueError,
            IndexError, KeyError, TypeError, OverflowError, struct.error, UserWarning) as exc:
        raise PhotoIntakeError("uploaded file is not a decodable image") from exc
    formats = ORIGINAL_FORMATS if allow_mpo else SUPPORTED_FORMATS
    if detected_format not in formats:
        raise UnsupportedPhotoFormatError(f"unsupported image format: {detected_format or 'unknown'}")
    if width <= 0 or height <= 0:
        raise PhotoIntakeError("uploaded image has invalid dimensions")
    mime, extension = formats[detected_format]
    return mime, extension, width, height


def inspect_supported_image(content: bytes) -> tuple[str, str, int, int]:
    """Strict processing/output boundary: never accept raw MPO here."""
    return _inspect_image(content, allow_mpo=False)


def inspect_original_image(content: bytes) -> tuple[str, str, int, int]:
    return _inspect_image(content, allow_mpo=True)


def store_immutable_bytes(content: bytes, destination: Path, checksum: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
            raise PhotoStorageIntegrityError("stored asset does not match its checksum identity")
        return destination
    descriptor, name = tempfile.mkstemp(dir=destination.parent, prefix=".photo-upload-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def normalize_image_for_processing(content: bytes) -> bytes:
    """Only MPO is re-encoded; all ordinary formats retain their exact bytes."""
    mime, _, _, _ = inspect_original_image(content)
    if mime != "image/mpo":
        return content
    with Image.open(BytesIO(content)) as image:
        image.seek(0)
        ImageOps.exif_transpose(image, in_place=True)
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            with image.convert("RGBA") as rgba:
                rgb = Image.new("RGB", rgba.size, "white")
                with rgba.getchannel("A") as alpha:
                    rgb.paste(rgba, mask=alpha)
        else:
            rgb = image.convert("RGB")
        with rgb:
            rgb.info.clear()  # Do not carry EXIF orientation or MPF into the JPEG.
            output = BytesIO()
            rgb.save(output, format="JPEG", quality=95, subsampling=0, optimize=False, progressive=False)
    return output.getvalue()


def _processing_asset(root: Path, record: dict) -> ProcessingImage:
    checksum = record["checksum_sha256"]
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise PhotoStorageIntegrityError("invalid processing asset checksum")
    path = root / "normalized" / checksum[:2] / f"{checksum}.jpg"
    if not path.resolve().is_relative_to(root.resolve()):
        raise PhotoStorageIntegrityError("processing asset is outside storage")
    content = path.read_bytes()
    mime, _, width, height = inspect_supported_image(content)
    if (hashlib.sha256(content).hexdigest() != checksum or mime != "image/jpeg"
            or (len(content), width, height) != (record["file_size_bytes"], record["width"], record["height"])):
        raise PhotoStorageIntegrityError("processing asset failed integrity verification")
    return ProcessingImage(path, checksum, mime, len(content), width, height)


def ensure_processing_representation(content: bytes, source_path: Path, source_checksum: str) -> ProcessingImage:
    mime, extension, width, height = inspect_original_image(content)
    if mime != "image/mpo":
        return ProcessingImage(source_path, source_checksum, mime, len(content), width, height)
    # Only a canonical original may determine the storage root for technical assets.
    root = source_path.resolve().parents[2]
    expected = root / "originals" / source_checksum[:2] / f"{source_checksum}{extension}"
    if source_path.resolve() != expected:
        raise PhotoStorageIntegrityError("MPO source path is not canonical")
    index = root / "normalized" / "index" / PROCESSING_VERSION / source_checksum[:2] / f"{source_checksum}.json"
    if not index.resolve().is_relative_to(root):
        raise PhotoStorageIntegrityError("processing index is outside storage")
    if index.exists():
        try:
            record = json.loads(index.read_text(encoding="utf-8"))
            if record["source_checksum_sha256"] != source_checksum or record["version"] != PROCESSING_VERSION:
                raise PhotoStorageIntegrityError("processing index has invalid source lineage")
            return _processing_asset(root, record)
        except (ValueError, KeyError, TypeError) as exc:
            raise PhotoStorageIntegrityError("processing index is invalid") from exc
    normalized = normalize_image_for_processing(content)
    normalized_mime, _, normalized_width, normalized_height = inspect_supported_image(normalized)
    checksum = hashlib.sha256(normalized).hexdigest()
    destination = root / "normalized" / checksum[:2] / f"{checksum}.jpg"
    if not destination.resolve().is_relative_to(root):
        raise PhotoStorageIntegrityError("processing asset is outside storage")
    store_immutable_bytes(normalized, destination, checksum)
    record = {
        "version": PROCESSING_VERSION, "source_checksum_sha256": source_checksum,
        "checksum_sha256": checksum, "file_size_bytes": len(normalized),
        "width": normalized_width, "height": normalized_height,
    }
    index_bytes = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    store_immutable_bytes(index_bytes, index, hashlib.sha256(index_bytes).hexdigest())
    return ProcessingImage(destination, checksum, normalized_mime, len(normalized), normalized_width, normalized_height)


def resolve_photo_for_processing(photo, *, originals_dir: Path | None = None) -> ProcessingImage:
    """Verify original identity first, then resolve its processing-safe asset."""
    if not photo.is_original:
        raise PhotoIntakeError("referenced Photo is not an original asset")
    path = Path(photo.file_path)
    path = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    content = path.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    if checksum != photo.checksum_sha256.lower():
        raise PhotoStorageIntegrityError("original Photo failed checksum verification")
    mime, extension, width, height = inspect_original_image(content)
    for stored, actual in ((photo.mime_type, mime), (photo.file_size_bytes, len(content)),
                           (photo.width, width), (photo.height, height)):
        if stored is not None and stored != actual:
            raise PhotoStorageIntegrityError("original Photo failed metadata verification")
    if originals_dir is not None:
        expected = originals_dir.resolve() / checksum[:2] / f"{checksum}{extension}"
        if path != expected:
            raise PhotoStorageIntegrityError("original Photo path is not canonical")
    return ensure_processing_representation(content, path, checksum)
