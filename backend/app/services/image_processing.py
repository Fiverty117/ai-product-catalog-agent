"""Original image validation and deterministic processing representations.

MPO is a source format, never a processing format. Frame 0 is Pillow's default
primary image. A versioned source index refers to a content-addressed canonical
asset; neither the index nor that asset is a Photo or an AI DerivedImage.
"""

import hashlib
import json
import logging
import os
import re
import struct
import tempfile
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESSING_VERSION = "image-processing-v3"
LEGACY_PROCESSING_VERSION = "image-processing-v2"
LEGACY_MPO_PROCESSING_VERSION = "image-processing-v1"
EXIF_ORIENTATION_TAG = 274
SUPPORTED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
ORIGINAL_FORMATS = {**SUPPORTED_FORMATS, "MPO": ("image/mpo", ".mpo")}
FORMAT_BY_MIME = {
    mime_type: (image_format, extension)
    for image_format, (mime_type, extension) in SUPPORTED_FORMATS.items()
}
logger = logging.getLogger(__name__)
ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


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


@dataclass(frozen=True)
class _ImageInspection:
    detected_format: str
    mime_type: str
    extension: str
    width: int
    height: int
    orientation: int
    requires_normalization: bool
    legacy_processing_compatible: bool


def _inspect_image_details(content: bytes, *, allow_mpo: bool) -> _ImageInspection:
    if not content:
        raise PhotoIntakeError("uploaded image is empty")
    detected_format: str | None = None
    stage = "open"
    try:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            warnings.filterwarnings(
                "error", category=Image.DecompressionBombWarning
            )
            with Image.open(BytesIO(content)) as image:
                detected_format = image.format
                has_mpf_metadata = "mp" in image.info
                width, height = image.size
                stage = "verify"
                image.verify()
            stage = "decode"
            orientation = 1
            malformed_orientation = False
            legacy_processing_compatible = True
            with Image.open(BytesIO(content)) as image:
                if detected_format == "MPO":
                    # Validate the container and each frame, without keeping decoded copies.
                    entries = image.mpinfo[0xB002]
                    if not entries:
                        raise ValueError("MPO has no frames")
                    for index, entry in enumerate(entries):
                        image.seek(index)
                        # Image.open checks frame 0; MPO.seek does not repeat that check.
                        if Image.MAX_IMAGE_PIXELS is not None and image.width * image.height > Image.MAX_IMAGE_PIXELS:
                            raise ValueError("MPO frame exceeds decoded pixel safety limit")
                        offset = image.offset
                        if entry["Size"] < 4 or offset < 0 or offset + entry["Size"] > len(content):
                            raise ValueError("invalid MPO frame bounds")
                        image.load()
                        if index == 0:
                            orientation, malformed_orientation, legacy_processing_compatible = (
                                _read_exif_orientation(image)
                            )
                else:
                    image.load()
                    (
                        orientation,
                        malformed_orientation,
                        legacy_processing_compatible,
                    ) = _read_exif_orientation(image)
            malformed_mpo_fallback = detected_format == "JPEG" and (
                has_mpf_metadata
                or any(
                    "Image appears to be a malformed MPO" in str(item.message)
                    for item in caught_warnings
                )
            )
    except (Image.DecompressionBombError, Image.DecompressionBombWarning,
            UnidentifiedImageError, OSError, SyntaxError, ValueError,
            IndexError, KeyError, TypeError, OverflowError, RuntimeError,
            struct.error, UserWarning) as exc:
        logger.warning(
            "image decode failed stage=%s detected_format=%s detected_mime=%s exception_type=%s",
            stage,
            detected_format or "unknown",
            ORIGINAL_FORMATS.get(detected_format, ("unknown", ""))[0],
            type(exc).__name__,
        )
        raise PhotoIntakeError("uploaded file is not a decodable image") from exc
    formats = ORIGINAL_FORMATS if allow_mpo else SUPPORTED_FORMATS
    if detected_format not in formats:
        raise UnsupportedPhotoFormatError(f"unsupported image format: {detected_format or 'unknown'}")
    if width <= 0 or height <= 0:
        raise PhotoIntakeError("uploaded image has invalid dimensions")
    mime, extension = formats[detected_format]
    return _ImageInspection(
        detected_format=detected_format,
        mime_type=mime,
        extension=extension,
        width=width,
        height=height,
        orientation=orientation,
        requires_normalization=(
            detected_format == "MPO"
            or malformed_mpo_fallback
            or malformed_orientation
            or orientation != 1
        ),
        legacy_processing_compatible=legacy_processing_compatible,
    )


def _read_exif_orientation(image: Image.Image) -> tuple[int, bool, bool]:
    if image.format == "PNG" and not (
        "exif" in image.info or "Raw profile type exif" in image.info
    ):
        # Pillow synthesizes EXIF Orientation from XMP when a PNG has no eXIf
        # chunk. Windows commonly writes already-oriented PNG pixels while
        # retaining the source JPEG's stale tiff:Orientation in XMP. Explorer
        # and browsers display those raw pixels; applying the XMP value rotates
        # them a second time. Only container-native PNG EXIF is authoritative,
        # but XMP-only Orientation must still be stripped from processing bytes
        # so an AI consumer cannot reinterpret it later.
        try:
            has_xmp_orientation = EXIF_ORIENTATION_TAG in image.getexif()
        except (OSError, SyntaxError, ValueError, IndexError, KeyError, TypeError,
                OverflowError, struct.error):
            return 1, True, False
        return 1, has_xmp_orientation, not has_xmp_orientation
    try:
        exif = image.getexif()
        value = exif.get(EXIF_ORIENTATION_TAG, 1)
    except (OSError, SyntaxError, ValueError, IndexError, KeyError, TypeError,
            OverflowError, struct.error):
        return 1, True, True
    if type(value) is int and 1 <= value <= 8:
        return value, False, True
    return 1, value not in (None, 1), True


def _inspect_image(content: bytes, *, allow_mpo: bool) -> tuple[str, str, int, int]:
    inspection = _inspect_image_details(content, allow_mpo=allow_mpo)
    return (
        inspection.mime_type,
        inspection.extension,
        inspection.width,
        inspection.height,
    )


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
    """Physically apply orientation only when a processing derivative is needed."""
    inspection = _inspect_image_details(content, allow_mpo=True)
    if not inspection.requires_normalization:
        return content
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Image appears to be a malformed MPO.*"
        )
        warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
        with Image.open(BytesIO(content)) as image:
            if inspection.detected_format == "MPO":
                image.seek(0)
            image.load()
            method = ORIENTATION_TRANSPOSE.get(inspection.orientation)
            normalized = image.transpose(method) if method is not None else image.copy()
            with normalized:
                return _encode_processing_image(
                    normalized,
                    source_format=inspection.detected_format,
                )


def _encode_processing_image(
    image: Image.Image,
    *,
    source_format: str,
) -> bytes:
    output = BytesIO()
    if source_format in {"JPEG", "MPO"}:
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            with image.convert("RGBA") as rgba:
                prepared = Image.new("RGB", rgba.size, "white")
                with rgba.getchannel("A") as alpha:
                    prepared.paste(rgba, mask=alpha)
        else:
            prepared = image.convert("RGB")
        with prepared:
            prepared.info.clear()
            prepared.save(
                output,
                format="JPEG",
                quality=95,
                subsampling=0,
                optimize=False,
                progressive=False,
            )
    elif source_format == "PNG":
        prepared = image.copy()
        with prepared:
            prepared.info.clear()
            prepared.save(output, format="PNG", optimize=False, compress_level=6)
    elif source_format == "WEBP":
        prepared = image.convert(
            "RGBA"
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info
            else "RGB"
        )
        with prepared:
            prepared.info.clear()
            prepared.save(
                output,
                format="WEBP",
                lossless=True,
                quality=100,
                method=0,
                exact=True,
            )
    else:  # pragma: no cover - guarded by strict inspection.
        raise UnsupportedPhotoFormatError(
            f"unsupported image format: {source_format}"
        )
    return output.getvalue()


def _processing_asset(root: Path, record: dict) -> ProcessingImage:
    checksum = record["checksum_sha256"]
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise PhotoStorageIntegrityError("invalid processing asset checksum")
    mime_type = record.get("mime_type", "image/jpeg")
    format_and_extension = FORMAT_BY_MIME.get(mime_type)
    if format_and_extension is None:
        raise PhotoStorageIntegrityError("invalid processing asset MIME type")
    _, extension = format_and_extension
    path = root / "normalized" / checksum[:2] / f"{checksum}{extension}"
    if not path.resolve().is_relative_to(root.resolve()):
        raise PhotoStorageIntegrityError("processing asset is outside storage")
    content = path.read_bytes()
    mime, _, width, height = inspect_supported_image(content)
    if (hashlib.sha256(content).hexdigest() != checksum or mime != mime_type
            or (len(content), width, height) != (record["file_size_bytes"], record["width"], record["height"])):
        raise PhotoStorageIntegrityError("processing asset failed integrity verification")
    return ProcessingImage(path, checksum, mime, len(content), width, height)


def ensure_processing_representation(content: bytes, source_path: Path, source_checksum: str) -> ProcessingImage:
    inspection = _inspect_image_details(content, allow_mpo=True)
    if not inspection.requires_normalization:
        return ProcessingImage(
            source_path,
            source_checksum,
            inspection.mime_type,
            len(content),
            inspection.width,
            inspection.height,
        )
    # Only a canonical original may determine the storage root for technical assets.
    root = source_path.resolve().parents[2]
    expected = (
        root
        / "originals"
        / source_checksum[:2]
        / f"{source_checksum}{inspection.extension}"
    )
    if source_path.resolve() != expected:
        raise PhotoStorageIntegrityError("normalizable source path is not canonical")
    existing = _load_processing_asset(
        root,
        source_checksum=source_checksum,
        version=PROCESSING_VERSION,
    )
    if existing is not None:
        return existing
    if inspection.legacy_processing_compatible:
        legacy = _load_processing_asset(
            root,
            source_checksum=source_checksum,
            version=LEGACY_PROCESSING_VERSION,
        )
        if legacy is not None:
            _store_processing_index(
                root,
                source_checksum=source_checksum,
                asset=legacy,
            )
            return legacy
    if inspection.detected_format == "MPO":
        legacy = _load_processing_asset(
            root,
            source_checksum=source_checksum,
            version=LEGACY_MPO_PROCESSING_VERSION,
        )
        if legacy is not None:
            _store_processing_index(
                root,
                source_checksum=source_checksum,
                asset=legacy,
            )
            return legacy
    normalized = normalize_image_for_processing(content)
    normalized_mime, normalized_extension, normalized_width, normalized_height = (
        inspect_supported_image(normalized)
    )
    checksum = hashlib.sha256(normalized).hexdigest()
    destination = (
        root
        / "normalized"
        / checksum[:2]
        / f"{checksum}{normalized_extension}"
    )
    if not destination.resolve().is_relative_to(root):
        raise PhotoStorageIntegrityError("processing asset is outside storage")
    store_immutable_bytes(normalized, destination, checksum)
    asset = ProcessingImage(
        destination,
        checksum,
        normalized_mime,
        len(normalized),
        normalized_width,
        normalized_height,
    )
    _store_processing_index(
        root,
        source_checksum=source_checksum,
        asset=asset,
    )
    return asset


def _processing_index(root: Path, source_checksum: str, version: str) -> Path:
    index = (
        root
        / "normalized"
        / "index"
        / version
        / source_checksum[:2]
        / f"{source_checksum}.json"
    )
    if not index.resolve().is_relative_to(root.resolve()):
        raise PhotoStorageIntegrityError("processing index is outside storage")
    return index


def _load_processing_asset(
    root: Path,
    *,
    source_checksum: str,
    version: str,
) -> ProcessingImage | None:
    index = _processing_index(root, source_checksum, version)
    if not index.exists():
        return None
    try:
        record = json.loads(index.read_text(encoding="utf-8"))
        if (
            record["source_checksum_sha256"] != source_checksum
            or record["version"] != version
        ):
            raise PhotoStorageIntegrityError(
                "processing index has invalid source lineage"
            )
        return _processing_asset(root, record)
    except (ValueError, KeyError, TypeError) as exc:
        raise PhotoStorageIntegrityError("processing index is invalid") from exc


def _store_processing_index(
    root: Path,
    *,
    source_checksum: str,
    asset: ProcessingImage,
) -> None:
    record = {
        "version": PROCESSING_VERSION,
        "source_checksum_sha256": source_checksum,
        "checksum_sha256": asset.checksum_sha256,
        "mime_type": asset.mime_type,
        "file_size_bytes": asset.file_size_bytes,
        "width": asset.width,
        "height": asset.height,
    }
    index_bytes = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    index = _processing_index(root, source_checksum, PROCESSING_VERSION)
    store_immutable_bytes(
        index_bytes,
        index,
        hashlib.sha256(index_bytes).hexdigest(),
    )


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
