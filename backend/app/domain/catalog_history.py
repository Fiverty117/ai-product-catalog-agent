"""Read-only, UI-oriented views over frozen catalog publication records."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.domain.schemas import CatalogBuildArtifactSummary, StrictSchema


HistoryStatus = Literal["queued", "running", "succeeded", "failed"]
FeatureState = Literal["unavailable", "invalid", "disabled", "enabled"]


class HistoryPublisher(StrictSchema):
    key: str
    display_name: str
    primary_color: str
    accent_color: str
    contact_text: str | None = None
    social_handle: str | None = None
    logo_present: bool


class HistoryContact(StrictSchema):
    kind: str
    value: str
    href: str | None


class HistoryFeature(StrictSchema):
    state: FeatureState
    key: str | None = None
    version: str | None = None
    display_name: str | None = None
    title: str | None = None
    subtitle: str | None = None
    edition_label: str | None = None
    heading: str | None = None
    note: str | None = None
    show_publisher_logo: bool = False
    hero_present: bool = False
    contacts: list[HistoryContact] = Field(default_factory=list)
    qr_target_type: str | None = None
    qr_target_url: str | None = None


class HistoryVariant(StrictSchema):
    external_sku: str | None
    flavor: str | None
    size_value: str | None
    size_unit: str | None
    servings: int | None
    price_amount: str
    price_currency: str


class HistoryProduct(StrictSchema):
    category_name: str
    brand_name: str
    product_name: str
    short_description: str | None
    variants: list[HistoryVariant]


class HistoryAttempt(StrictSchema):
    attempt: int = Field(ge=1)
    status: Literal["running", "succeeded", "failed"]
    started_at: datetime
    completed_at: datetime | None
    page_count: int | None
    error: str | None
    render_version: str


class CatalogHistoryItem(StrictSchema):
    build_id: uuid.UUID
    status: HistoryStatus
    created_at: datetime
    completed_at: datetime | None
    render_version: str
    publisher: HistoryPublisher | None
    product_count: int | None
    sku_count: int | None
    layout_key: str | None
    layout_version: str | None
    layout_display_name: str | None
    theme_key: str | None
    theme_version: str | None
    theme_display_name: str | None
    palette_source: str | None
    primary_color: str | None
    accent_color: str | None
    cover: HistoryFeature
    closing: HistoryFeature
    page_count: int | None
    artifact_available: bool
    artifact: CatalogBuildArtifactSummary | None
    latest_render_status: str | None
    error: str | None
    historical_data_available: bool


class CatalogHistoryDetail(CatalogHistoryItem):
    snapshot_schema_version: str | None
    currency: str | None
    as_of: datetime | None
    products: list[HistoryProduct]
    render_attempts: list[HistoryAttempt]


class CatalogHistoryPage(StrictSchema):
    items: list[CatalogHistoryItem]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    all_total: int = Field(ge=0)


class CatalogHistoryOptions(StrictSchema):
    publishers: list[str]
    themes: list[str]
