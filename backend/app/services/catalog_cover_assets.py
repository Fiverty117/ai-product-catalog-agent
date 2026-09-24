"""Immutable, content-addressed local cover Hero uploads."""

import hashlib
import os
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogCoverAsset
from app.domain.schemas import FrozenCatalogCoverHero
from app.services.photo_intake import PhotoIntakeError, inspect_supported_image

DEFAULT_STORAGE_ROOT = Path(__file__).resolve().parents[3] / "storage"
MAX_COVER_HERO_BYTES = 10 * 1024 * 1024
MAX_COVER_HERO_PIXELS = 24_000_000


class CatalogCoverAssetError(ValueError):
    pass


class UnknownCatalogCoverAssetError(CatalogCoverAssetError):
    pass


class CatalogCoverAssetIntegrityError(CatalogCoverAssetError):
    pass


class CatalogCoverAssetTooLargeError(CatalogCoverAssetError):
    pass


def _relative_path(checksum: str, extension: str) -> str:
    return f"covers/{checksum[:2]}/{checksum}{extension}"


def ingest_catalog_cover_asset(
    session: Session,
    content: bytes,
    *,
    declared_mime_type: str,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> CatalogCoverAsset:
    if len(content) > MAX_COVER_HERO_BYTES:
        raise CatalogCoverAssetTooLargeError("cover Hero image exceeds 10 MiB")
    try:
        with Image.open(BytesIO(content)) as image:
            candidate_width, candidate_height = image.size
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError):
        raise CatalogCoverAssetError("cover Hero must be a decodable PNG, JPEG or WEBP image") from None
    if candidate_width * candidate_height > MAX_COVER_HERO_PIXELS:
        raise CatalogCoverAssetTooLargeError("cover Hero image dimensions are too large")
    try:
        mime, extension, width, height = inspect_supported_image(content)
    except PhotoIntakeError:
        raise CatalogCoverAssetError("cover Hero must be a decodable PNG, JPEG or WEBP image") from None
    if mime != declared_mime_type:
        raise CatalogCoverAssetError("cover Hero MIME type does not match image content")
    checksum = hashlib.sha256(content).hexdigest()
    root = storage_root.resolve()
    destination = root / _relative_path(checksum, extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != content:
            raise CatalogCoverAssetIntegrityError("stored cover Hero does not match its content identity")
    else:
        descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent, prefix=".cover-hero-", suffix=".tmp")
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
    existing = session.scalar(select(CatalogCoverAsset).where(
        CatalogCoverAsset.checksum_sha256 == checksum,
        CatalogCoverAsset.mime_type == mime,
        CatalogCoverAsset.file_path == str(destination),
    ))
    if existing is not None:
        freeze_catalog_cover_asset(existing, storage_root=root)
        return existing
    asset = CatalogCoverAsset(
        file_path=str(destination), checksum_sha256=checksum, mime_type=mime,
        file_size_bytes=len(content), width=width, height=height,
    )
    session.add(asset)
    session.flush()
    return asset


def freeze_catalog_cover_asset(asset: CatalogCoverAsset, *, storage_root: Path = DEFAULT_STORAGE_ROOT) -> FrozenCatalogCoverHero:
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(asset.mime_type)
    if extension is None:
        raise CatalogCoverAssetIntegrityError("cover Hero MIME type is unsupported")
    relative = _relative_path(asset.checksum_sha256.lower(), extension)
    root = storage_root.resolve()
    path = Path(asset.file_path).resolve()
    if path != root / relative or not path.is_relative_to(root):
        raise CatalogCoverAssetIntegrityError("cover Hero path is not canonical")
    frozen = FrozenCatalogCoverHero(
        source_cover_asset_id=asset.id, checksum_sha256=asset.checksum_sha256.lower(),
        mime_type=asset.mime_type, file_size_bytes=asset.file_size_bytes,
        width=asset.width, height=asset.height, storage_relative_path=relative,
    )
    load_frozen_catalog_cover_hero(frozen, storage_root=root)
    return frozen


def load_frozen_catalog_cover_hero(hero: FrozenCatalogCoverHero, *, storage_root: Path = DEFAULT_STORAGE_ROOT) -> bytes:
    root = storage_root.resolve()
    path = (root / hero.storage_relative_path).resolve()
    if not path.is_relative_to(root) or path.relative_to(root).as_posix() != hero.storage_relative_path:
        raise CatalogCoverAssetIntegrityError("frozen cover Hero path is outside canonical storage")
    try:
        content = path.read_bytes()
    except OSError:
        raise CatalogCoverAssetIntegrityError("frozen cover Hero is missing") from None
    if hashlib.sha256(content).hexdigest() != hero.checksum_sha256.lower() or len(content) != hero.file_size_bytes:
        raise CatalogCoverAssetIntegrityError("frozen cover Hero failed checksum verification")
    try:
        mime, _, width, height = inspect_supported_image(content)
    except PhotoIntakeError:
        raise CatalogCoverAssetIntegrityError("frozen cover Hero is corrupt") from None
    if (mime, width, height) != (hero.mime_type, hero.width, hero.height):
        raise CatalogCoverAssetIntegrityError("frozen cover Hero metadata mismatch")
    return content
