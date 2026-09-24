"""Versioned cover compositions and immutable edition resolution."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import CatalogCoverAsset
from app.domain.schemas import CatalogCoverCreate, FrozenCatalogCoverHero, ResolvedCatalogBranding, ResolvedCatalogCover
from app.services.catalog_cover_assets import CatalogCoverAssetIntegrityError, UnknownCatalogCoverAssetError, freeze_catalog_cover_asset

COVER_SCHEMA_VERSION = "catalog-cover-v1"


class UnknownCatalogCoverError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogCoverDefinition:
    key: str
    version: str
    display_name: str
    description: str
    css_class: str
    requires_hero: bool


_COVERS = (
    CatalogCoverDefinition("minimal", "1", "Minimal", "Clean and publisher-first", "cover-minimal", False),
    CatalogCoverDefinition("editorial", "1", "Editorial", "Structured and expressive", "cover-editorial", False),
    CatalogCoverDefinition("hero", "1", "Hero", "Image-led presentation", "cover-hero-layout", True),
)


def catalog_cover_definitions() -> tuple[CatalogCoverDefinition, ...]:
    return _COVERS


def resolve_catalog_cover_definition(key: str, version: str) -> CatalogCoverDefinition:
    for item in _COVERS:
        if item.key == key and item.version == version:
            return item
    raise UnknownCatalogCoverError(f"unsupported catalog cover: {key}/{version}")


def resolve_catalog_cover(
    choice: CatalogCoverCreate,
    branding: ResolvedCatalogBranding,
    session: Session,
    *,
    storage_root: Path,
) -> ResolvedCatalogCover:
    if not choice.enabled:
        return ResolvedCatalogCover(schema_version=COVER_SCHEMA_VERSION, enabled=False)
    definition = resolve_catalog_cover_definition(choice.cover_key or "", choice.cover_version or "")
    hero: FrozenCatalogCoverHero | None = None
    if choice.hero_asset_id is not None:
        asset = session.get(CatalogCoverAsset, choice.hero_asset_id)
        if asset is None:
            raise UnknownCatalogCoverAssetError("cover Hero asset not found")
        hero = freeze_catalog_cover_asset(asset, storage_root=storage_root)
    if definition.requires_hero and hero is None:
        raise CatalogCoverAssetIntegrityError("Hero cover requires an uploaded image")
    return ResolvedCatalogCover(
        schema_version=COVER_SCHEMA_VERSION, enabled=True,
        cover_key=definition.key, cover_version=definition.version,
        title=choice.title, subtitle=choice.subtitle, edition_label=choice.edition_label,
        show_publisher_logo=bool(choice.show_publisher_logo if choice.show_publisher_logo is not None else True) and branding.logo is not None,
        hero=hero,
    )


def hash_resolved_catalog_cover(cover: ResolvedCatalogCover) -> str:
    visual = cover.model_dump(mode="json")
    if visual["hero"] is not None:
        visual["hero"].pop("source_cover_asset_id")
    canonical = json.dumps(visual, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
