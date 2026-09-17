"""Small deterministic publisher-branding boundary for catalog renders."""

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogBrandAsset, CatalogBrandProfile
from app.domain.schemas import (
    CatalogBrandProfileCreate,
    CatalogBrandProfileUpdate,
    FrozenCatalogBrandLogo,
    ResolvedCatalogBranding,
)
from app.services.photo_intake import PhotoIntakeError, inspect_supported_image

BRANDING_SCHEMA_VERSION = "catalog-branding-v1"
DEFAULT_STORAGE_ROOT = Path(__file__).resolve().parents[3] / "storage"


class CatalogBrandingError(ValueError):
    pass


class UnknownCatalogBrandProfileError(CatalogBrandingError):
    pass


class InactiveCatalogBrandProfileError(CatalogBrandingError):
    pass


class CatalogBrandLogoIntegrityError(CatalogBrandingError):
    pass


def _brand_logo_locator(checksum: str, extension: str) -> str:
    return f"branding/{checksum[:2]}/{checksum}{extension}"


def ingest_catalog_brand_logo(
    session: Session,
    image_bytes: bytes,
    *,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> CatalogBrandAsset:
    try:
        mime, extension, width, height = inspect_supported_image(image_bytes)
    except PhotoIntakeError:
        raise CatalogBrandLogoIntegrityError("logo is corrupt or unsupported") from None
    checksum = hashlib.sha256(image_bytes).hexdigest()
    root = storage_root.resolve()
    destination = root / _brand_logo_locator(checksum, extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != image_bytes:
            raise CatalogBrandLogoIntegrityError("existing brand logo does not match its content identity")
    else:
        descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent, prefix=".brand-logo-", suffix=".tmp")
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(image_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        if destination.read_bytes() != image_bytes:
            raise CatalogBrandLogoIntegrityError("stored brand logo failed verification")
    existing = session.scalar(select(CatalogBrandAsset).where(
        CatalogBrandAsset.checksum_sha256 == checksum,
        CatalogBrandAsset.mime_type == mime,
        CatalogBrandAsset.file_path == str(destination),
    ))
    if existing is not None:
        _verify_asset_row(existing, storage_root=root)
        return existing
    asset = CatalogBrandAsset(
        file_path=str(destination), checksum_sha256=checksum, mime_type=mime,
        file_size_bytes=len(image_bytes), width=width, height=height,
    )
    session.add(asset)
    session.flush()
    return asset


def create_catalog_brand_profile(
    session: Session,
    data: CatalogBrandProfileCreate | dict[str, Any],
    *,
    logo_asset: CatalogBrandAsset | None = None,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> CatalogBrandProfile:
    try:
        value = CatalogBrandProfileCreate.model_validate(data)
    except ValidationError:
        raise CatalogBrandingError("invalid catalog brand profile fields") from None
    if logo_asset is not None:
        _verify_asset_row(logo_asset, storage_root=storage_root)
    profile = CatalogBrandProfile(**value.model_dump(), logo_asset=logo_asset, is_active=True)
    session.add(profile)
    session.flush()
    return profile


def update_catalog_brand_profile(
    session: Session,
    profile: CatalogBrandProfile,
    data: CatalogBrandProfileUpdate | dict[str, Any],
    *,
    logo_asset: CatalogBrandAsset | None = None,
    change_logo: bool = False,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> CatalogBrandProfile:
    try:
        value = CatalogBrandProfileUpdate.model_validate(data)
    except ValidationError:
        raise CatalogBrandingError("invalid catalog brand profile update") from None
    for name, field_value in value.model_dump(exclude_unset=True).items():
        setattr(profile, name, field_value)
    if change_logo:
        if logo_asset is not None:
            _verify_asset_row(logo_asset, storage_root=storage_root)
        profile.logo_asset = logo_asset
    session.flush()
    return profile


def get_active_catalog_brand_profile(
    session: Session, *, key: str | None = None, profile_id: uuid.UUID | None = None,
) -> CatalogBrandProfile:
    if (key is None) == (profile_id is None):
        raise CatalogBrandingError("select exactly one profile key or ID")
    profile = session.scalar(select(CatalogBrandProfile).where(CatalogBrandProfile.key == key)) if key is not None else session.get(CatalogBrandProfile, profile_id)
    if profile is None:
        raise UnknownCatalogBrandProfileError("catalog brand profile not found")
    if not profile.is_active:
        raise InactiveCatalogBrandProfileError("inactive catalog brand profile cannot be used for a new render")
    return profile


def resolve_catalog_branding(
    profile: CatalogBrandProfile,
    *, storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> ResolvedCatalogBranding:
    if not profile.is_active:
        raise InactiveCatalogBrandProfileError("inactive catalog brand profile cannot be used for a new render")
    logo = None
    if profile.logo_asset_id is not None:
        asset = profile.logo_asset
        if asset is None:
            raise CatalogBrandLogoIntegrityError("catalog brand logo asset row is missing")
        relative = _verify_asset_row(asset, storage_root=storage_root)
        logo = FrozenCatalogBrandLogo(
            source_brand_asset_id=asset.id,
            checksum_sha256=asset.checksum_sha256.lower(), mime_type=asset.mime_type,
            file_size_bytes=asset.file_size_bytes, width=asset.width, height=asset.height,
            storage_relative_path=relative,
        )
    try:
        return ResolvedCatalogBranding(
            schema_version=BRANDING_SCHEMA_VERSION, source_profile_id=profile.id,
            profile_key=profile.key, display_name=profile.display_name,
            primary_color=profile.primary_color, accent_color=profile.accent_color,
            contact_text=profile.contact_text, social_handle=profile.social_handle,
            logo=logo,
        )
    except ValidationError:
        raise CatalogBrandingError("catalog brand profile contains invalid render values") from None


def hash_resolved_catalog_branding(branding: ResolvedCatalogBranding) -> str:
    # Profile UUID/key and logo row UUID are audit lineage, not visual content identity.
    logo = branding.logo.model_dump(mode="json", exclude={"source_brand_asset_id"}) if branding.logo else None
    visual = {
        "schema_version": branding.schema_version,
        "display_name": branding.display_name,
        "primary_color": branding.primary_color,
        "accent_color": branding.accent_color,
        "contact_text": branding.contact_text,
        "social_handle": branding.social_handle,
        "logo": logo,
    }
    canonical = json.dumps(visual, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_frozen_catalog_brand_logo(
    logo: FrozenCatalogBrandLogo, *, storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> bytes:
    root = storage_root.resolve()
    path = (root / logo.storage_relative_path).resolve()
    if not path.is_relative_to(root) or path.relative_to(root).as_posix() != logo.storage_relative_path:
        raise CatalogBrandLogoIntegrityError("frozen brand logo locator is outside canonical storage")
    try:
        content = path.read_bytes()
    except OSError:
        raise CatalogBrandLogoIntegrityError("frozen brand logo is missing") from None
    _verify_logo_bytes(content, logo.checksum_sha256, logo.mime_type, logo.file_size_bytes, logo.width, logo.height)
    return content


def _verify_asset_row(asset: CatalogBrandAsset, *, storage_root: Path = DEFAULT_STORAGE_ROOT) -> str:
    root = storage_root.resolve()
    path = Path(asset.file_path).resolve()
    relative = _brand_logo_locator(asset.checksum_sha256.lower(), {
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    }.get(asset.mime_type, ""))
    if not path.is_relative_to(root) or path != root / relative:
        raise CatalogBrandLogoIntegrityError("catalog brand logo path is not canonical")
    try:
        content = path.read_bytes()
    except OSError:
        raise CatalogBrandLogoIntegrityError("catalog brand logo file is missing") from None
    _verify_logo_bytes(content, asset.checksum_sha256, asset.mime_type, asset.file_size_bytes, asset.width, asset.height)
    return relative


def _verify_logo_bytes(content: bytes, checksum: str, mime: str, size: int, width: int, height: int) -> None:
    if hashlib.sha256(content).hexdigest() != checksum.lower():
        raise CatalogBrandLogoIntegrityError("catalog brand logo checksum mismatch")
    try:
        detected_mime, _, detected_width, detected_height = inspect_supported_image(content)
    except PhotoIntakeError:
        raise CatalogBrandLogoIntegrityError("catalog brand logo is corrupt or unsupported") from None
    if (detected_mime, len(content), detected_width, detected_height) != (mime, size, width, height):
        raise CatalogBrandLogoIntegrityError("catalog brand logo metadata mismatch")
