"""Typed, read-only plan for initializing an editable Builder from History."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.domain.schemas import CatalogReadinessIssue, CatalogClosingCreate, StrictSchema


class DuplicateSource(StrictSchema):
    build_id: uuid.UUID
    created_at: datetime
    status: Literal["queued", "running", "succeeded", "failed"]
    render_version: str
    historical_publisher_name: str | None
    historical_product_count: int | None


class DuplicateProduct(StrictSchema):
    product_id: uuid.UUID
    status: Literal["ready", "not_ready", "unavailable"]
    historical_name: str
    historical_brand_name: str
    current_name: str | None = None
    current_brand_name: str | None = None
    blockers: list[CatalogReadinessIssue] = Field(default_factory=list)


class DuplicatePublisher(StrictSchema):
    state: Literal["selected", "unavailable"]
    historical_name: str
    current_profile_id: uuid.UUID | None = None
    current_name: str | None = None


class DuplicateVersionChoice(StrictSchema):
    state: Literal["copied", "defaulted", "unavailable"]
    key: str | None = None
    version: str | None = None


class DuplicatePalette(StrictSchema):
    state: Literal["copied", "unresolved"]
    source: Literal["publisher", "custom", "unresolved"]
    primary_color_override: str | None = None
    accent_color_override: str | None = None
    historical_resolved_primary: str | None = None
    historical_resolved_accent: str | None = None


class DuplicateHero(StrictSchema):
    asset_id: uuid.UUID
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int
    height: int
    preview_url: str


class DuplicateCover(StrictSchema):
    enabled: bool = False
    state: Literal["copied", "defaulted", "unavailable"] = "defaulted"
    key: str | None = None
    version: str | None = None
    title: str | None = None
    subtitle: str | None = None
    edition_label: str | None = None
    show_publisher_logo: bool = False
    hero: DuplicateHero | None = None
    hero_unavailable: bool = False


class DuplicateClosing(StrictSchema):
    state: Literal["copied", "defaulted", "unavailable"] = "defaulted"
    choice: CatalogClosingCreate = Field(default_factory=lambda: CatalogClosingCreate(enabled=False))


class DuplicateNotice(StrictSchema):
    code: Literal[
        "product_not_ready", "product_unavailable", "publisher_unavailable",
        "layout_version_unavailable", "theme_version_unavailable",
        "cover_version_unavailable", "closing_version_unavailable",
        "hero_asset_unavailable", "publisher_contact_unavailable",
        "publisher_contact_provenance_unavailable", "palette_override_provenance_unavailable",
        "logo_choice_provenance_unavailable",
        "source_predates_theme", "source_predates_cover", "source_predates_closing",
        "no_ready_products", "source_currency_defaulted",
    ]
    field: str
    product_id: uuid.UUID | None = None


class CatalogDuplicateTemplate(StrictSchema):
    source: DuplicateSource
    can_initialize: bool
    unavailable_reason: Literal["invalid_snapshot", "unsupported_configuration"] | None = None
    products: list[DuplicateProduct] = Field(default_factory=list)
    publisher: DuplicatePublisher | None = None
    layout: DuplicateVersionChoice | None = None
    theme: DuplicateVersionChoice | None = None
    palette: DuplicatePalette | None = None
    cover: DuplicateCover | None = None
    closing: DuplicateClosing | None = None
    defaults_applied: list[DuplicateNotice] = Field(default_factory=list)
    warnings: list[DuplicateNotice] = Field(default_factory=list)
