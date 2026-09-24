import base64
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from babel import Locale
from babel.core import UnknownLocaleError
from babel.dates import format_date
from babel.numbers import format_currency, format_decimal
from jinja2 import Environment, StrictUndefined, select_autoescape
from markupsafe import Markup
from pydantic import ValidationError
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.db.models import CatalogArtifact, CatalogRenderRun, CatalogSnapshot, Job
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus
from app.domain.schemas import (
    CatalogBrandingView,
    CatalogRenderConfig,
    CatalogRenderJobPayload,
    CatalogRenderJobPayloadV2,
    CatalogRenderJobPayloadV3,
    CatalogRenderJobPayloadV4,
    CatalogRenderLayoutView,
    CatalogRenderProductView,
    CatalogRenderSectionView,
    CatalogRenderVariantView,
    CatalogRenderViewModel,
    CatalogSnapshotData,
    CatalogVariantSnapshot,
    FrozenCatalogAsset,
    ResolvedCatalogBranding,
    ResolvedCatalogTheme,
    ResolvedCatalogCover,
    CatalogCoverCreate,
)
from app.rendering.catalog_layouts import (
    CatalogLayoutDefinition,
    UnknownCatalogLayoutError,
    resolve_catalog_layout,
)
from app.rendering.catalog_themes import hash_resolved_catalog_theme, resolve_catalog_theme, resolve_catalog_theme_definition
from app.rendering.catalog_covers import hash_resolved_catalog_cover, resolve_catalog_cover, resolve_catalog_cover_definition
from app.services.catalog_cover_assets import load_frozen_catalog_cover_hero
from app.services.catalog_branding import (
    get_active_catalog_brand_profile,
    hash_resolved_catalog_branding,
    load_frozen_catalog_brand_logo,
    resolve_catalog_branding,
)
from app.services.catalog_snapshots import (
    CatalogSnapshotError,
    read_catalog_snapshot_data,
)
from app.services.extraction import sanitize_extraction_error
from app.services.jobs import enqueue_job
from app.services.photo_intake import PhotoIntakeError, inspect_supported_image

CATALOG_RENDER_JOB_TYPE = "catalog.render.v1"
CATALOG_RENDER_JOB_TYPE_V2 = "catalog.render.v2"
CATALOG_RENDER_JOB_TYPE_V3 = "catalog.render.v3"
CATALOG_RENDER_JOB_TYPE_V4 = "catalog.render.v4"
CATALOG_RENDERER_VERSION = "catalog-chromium-v1"
CATALOG_RENDERER_ENGINE = "chromium"
PDF_MEDIA_TYPE = "application/pdf"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEMPLATE_ROOT = PROJECT_ROOT / "templates" / "grabelan"
DEFAULT_STORAGE_ROOT = PROJECT_ROOT / "storage"
DEFAULT_CATALOGS_DIR = DEFAULT_STORAGE_ROOT / "catalogs"


class CatalogRenderError(ValueError):
    pass


class InvalidCatalogRenderConfigError(CatalogRenderError):
    pass


class UnknownCatalogTemplateError(CatalogRenderError):
    pass


class InvalidCatalogTemplateError(CatalogRenderError):
    pass


class SnapshotAssetIntegrityError(CatalogRenderError):
    pass


class InvalidCatalogRenderJobError(CatalogRenderError):
    pass


class CompletedCatalogRenderRunError(CatalogRenderError):
    pass


class InvalidCatalogPdfError(CatalogRenderError):
    pass


class CatalogPdfStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogTemplate:
    key: str
    version: str
    display_name: str
    root: Path
    html_path: Path
    css_path: Path
    theme_css_path: Path | None = None
    cover_css_path: Path | None = None


@dataclass(frozen=True)
class StoredCatalogPdf:
    file_path: str
    checksum_sha256: str
    file_size_bytes: int
    page_count: int


def resolve_catalog_template(
    template_key: str,
    *,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
) -> CatalogTemplate:
    if template_key not in ("grabelan-catalog-v1", "grabelan-catalog-v2", "grabelan-catalog-v3"):
        raise UnknownCatalogTemplateError(
            f"unsupported catalog template: {template_key}"
        )
    themed = template_key in ("grabelan-catalog-v2", "grabelan-catalog-v3")
    with_cover = template_key == "grabelan-catalog-v3"
    template = CatalogTemplate(
        key=template_key, version="3.0" if with_cover else "2.0" if themed else "1.0", display_name="Grabelan",
        root=template_root.resolve(),
        html_path=(template_root / ("catalog-v3.html.jinja" if with_cover else "catalog-v2.html.jinja" if themed else "catalog-v1.html.jinja")).resolve(),
        css_path=(template_root / "catalog-v1.css").resolve(),
        theme_css_path=(template_root / "catalog-v2-themes.css").resolve() if themed else None,
        cover_css_path=(template_root / "catalog-v3-cover.css").resolve() if with_cover else None,
    )
    for path in (template.html_path, template.css_path, *([template.theme_css_path] if template.theme_css_path else []), *([template.cover_css_path] if template.cover_css_path else [])):
        if not path.is_file() or not path.is_relative_to(template.root):
            raise InvalidCatalogTemplateError(
                f"catalog template file is missing: {path.name}"
            )
    return template


def hash_catalog_template(template: CatalogTemplate) -> str:
    files = [template.html_path, template.css_path]
    if template.theme_css_path is not None:
        files.append(template.theme_css_path)
    if template.cover_css_path is not None:
        files.append(template.cover_css_path)
    static_root = template.root / "static"
    if static_root.is_dir():
        files.extend(path for path in static_root.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(
        files,
        key=lambda item: item.relative_to(template.root).as_posix(),
    ):
        logical_name = path.relative_to(template.root).as_posix().encode("utf-8")
        try:
            content = path.read_bytes()
        except OSError:
            raise InvalidCatalogTemplateError(
                f"catalog template file could not be read: {path.name}"
            ) from None
        digest.update(len(logical_name).to_bytes(8, "big"))
        digest.update(logical_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def normalize_catalog_render_config(
    config: CatalogRenderConfig | Mapping[str, Any] | None = None,
) -> CatalogRenderConfig:
    try:
        validated = CatalogRenderConfig.model_validate(config or {})
        locale = Locale.parse(validated.locale, sep="-")
    except (ValidationError, UnknownLocaleError, ValueError):
        raise InvalidCatalogRenderConfigError(
            "unsupported catalog render configuration or locale"
        ) from None
    normalized_locale = "-".join(
        part
        for part in (locale.language, locale.script, locale.territory, locale.variant)
        if part
    )
    return validated.model_copy(update={"locale": normalized_locale})


def hash_catalog_render_config(config: CatalogRenderConfig) -> str:
    # Layout has its own explicit semantic identity in catalog.render.v2. Keeping
    # it out of the legacy config hash preserves validation of already-enqueued
    # v1/v2 payloads created before layout selection existed.
    return hashlib.sha256(
        _canonical_json(config.model_dump(mode="json", exclude={"layout"})).encode("utf-8")
    ).hexdigest()


def resolve_catalog_render_layout(
    config: CatalogRenderConfig,
    *,
    require_registered_geometry: bool = True,
) -> CatalogLayoutDefinition:
    try:
        layout = resolve_catalog_layout(config.layout)
    except UnknownCatalogLayoutError as error:
        raise InvalidCatalogRenderConfigError(str(error)) from None
    if require_registered_geometry and (
        config.page_size != layout.page_size or config.orientation != layout.orientation
    ):
        raise InvalidCatalogRenderConfigError(
            f"catalog layout {layout.key} requires {layout.page_size} {layout.orientation}"
        )
    return layout


def build_catalog_render_idempotency_key(
    payload: CatalogRenderJobPayload | CatalogRenderJobPayloadV2 | CatalogRenderJobPayloadV3 | CatalogRenderJobPayloadV4,
    *,
    job_type: str = CATALOG_RENDER_JOB_TYPE,
) -> str:
    values = payload.model_dump(mode="json")
    if isinstance(payload, CatalogRenderJobPayloadV2):
        # Frozen data travels with the Job; its content hash and profile ID define
        # the logical visual/lineage identity without row-specific logo UUIDs.
        values.pop("branding_data")
    if isinstance(payload, CatalogRenderJobPayloadV4):
        # The cover hash captures visual content without upload-row UUIDs.
        values.pop("cover_data")
    identity = {"job_type": job_type, **values}
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"{job_type}:{digest}"


def enqueue_catalog_render_v2(
    session: Session,
    *,
    catalog_snapshot_id: uuid.UUID,
    brand_profile_id: uuid.UUID,
    config: CatalogRenderConfig | Mapping[str, Any] | None = None,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
    renderer_version: str = CATALOG_RENDERER_VERSION,
    max_attempts: int = 3,
) -> Job:
    normalized_config = normalize_catalog_render_config(config)
    layout = resolve_catalog_render_layout(normalized_config)
    template = resolve_catalog_template(normalized_config.template_key, template_root=template_root)
    snapshot = session.get(CatalogSnapshot, catalog_snapshot_id)
    if snapshot is None:
        raise InvalidCatalogRenderJobError(f"CatalogSnapshot not found: {catalog_snapshot_id}")
    try:
        read_catalog_snapshot_data(session, catalog_snapshot_id)
    except CatalogSnapshotError as error:
        raise InvalidCatalogRenderJobError(str(error)) from None
    profile = get_active_catalog_brand_profile(session, profile_id=brand_profile_id)
    branding = resolve_catalog_branding(profile, storage_root=storage_root)
    version = renderer_version.strip()
    if not version:
        raise InvalidCatalogRenderJobError("renderer_version is required")
    payload = CatalogRenderJobPayloadV2(
        catalog_snapshot_id=snapshot.id,
        snapshot_content_hash=snapshot.content_hash.lower(),
        snapshot_schema_version=snapshot.schema_version,
        template_key=template.key, template_version=template.version,
        template_hash=hash_catalog_template(template), renderer_version=version,
        config=normalized_config, config_hash=hash_catalog_render_config(normalized_config),
        catalog_brand_profile_id=profile.id,
        branding_schema_version=branding.schema_version,
        branding_hash=hash_resolved_catalog_branding(branding),
        branding_data=branding,
        layout_key=layout.key,
        layout_version=layout.version,
    )
    return enqueue_job(
        session, job_type=CATALOG_RENDER_JOB_TYPE_V2,
        payload=payload.model_dump(mode="json"),
        idempotency_key=build_catalog_render_idempotency_key(payload, job_type=CATALOG_RENDER_JOB_TYPE_V2),
        max_attempts=max_attempts,
    )


def enqueue_catalog_render_v3(
    session: Session, *, catalog_snapshot_id: uuid.UUID, brand_profile_id: uuid.UUID,
    theme_key: str, theme_version: str, primary_color_override: str | None = None,
    accent_color_override: str | None = None,
    config: CatalogRenderConfig | Mapping[str, Any] | None = None,
    template_root: Path = DEFAULT_TEMPLATE_ROOT, storage_root: Path = DEFAULT_STORAGE_ROOT,
    renderer_version: str = CATALOG_RENDERER_VERSION, max_attempts: int = 3,
) -> Job:
    normalized_config = normalize_catalog_render_config(config or {"template_key": "grabelan-catalog-v2"})
    if normalized_config.template_key != "grabelan-catalog-v2":
        raise InvalidCatalogRenderConfigError("themed catalog renders require the v2 template")
    layout = resolve_catalog_render_layout(normalized_config)
    template = resolve_catalog_template(normalized_config.template_key, template_root=template_root)
    snapshot = session.get(CatalogSnapshot, catalog_snapshot_id)
    if snapshot is None:
        raise InvalidCatalogRenderJobError(f"CatalogSnapshot not found: {catalog_snapshot_id}")
    read_catalog_snapshot_data(session, snapshot.id)
    profile = get_active_catalog_brand_profile(session, profile_id=brand_profile_id)
    branding = resolve_catalog_branding(profile, storage_root=storage_root)
    theme = resolve_catalog_theme(theme_key, theme_version, branding,
        primary_color_override=primary_color_override, accent_color_override=accent_color_override)
    version = renderer_version.strip()
    if not version:
        raise InvalidCatalogRenderJobError("renderer_version is required")
    payload = CatalogRenderJobPayloadV3(
        catalog_snapshot_id=snapshot.id, snapshot_content_hash=snapshot.content_hash.lower(),
        snapshot_schema_version=snapshot.schema_version,
        template_key=template.key, template_version=template.version,
        template_hash=hash_catalog_template(template), renderer_version=version,
        config=normalized_config, config_hash=hash_catalog_render_config(normalized_config),
        catalog_brand_profile_id=profile.id, branding_schema_version=branding.schema_version,
        branding_hash=hash_resolved_catalog_branding(branding), branding_data=branding,
        layout_key=layout.key, layout_version=layout.version,
        theme_schema_version=theme.schema_version, theme_hash=hash_resolved_catalog_theme(theme), theme_data=theme,
    )
    return enqueue_job(
        session, job_type=CATALOG_RENDER_JOB_TYPE_V3,
        payload=payload.model_dump(mode="json"),
        idempotency_key=build_catalog_render_idempotency_key(payload, job_type=CATALOG_RENDER_JOB_TYPE_V3),
        max_attempts=max_attempts,
    )


def enqueue_catalog_render_v4(
    session: Session, *, catalog_snapshot_id: uuid.UUID, brand_profile_id: uuid.UUID,
    theme_key: str, theme_version: str, cover_choice: CatalogCoverCreate,
    primary_color_override: str | None = None, accent_color_override: str | None = None,
    config: CatalogRenderConfig | Mapping[str, Any] | None = None,
    template_root: Path = DEFAULT_TEMPLATE_ROOT, storage_root: Path = DEFAULT_STORAGE_ROOT,
    renderer_version: str = CATALOG_RENDERER_VERSION, max_attempts: int = 3,
) -> Job:
    normalized_config = normalize_catalog_render_config(config or {"template_key": "grabelan-catalog-v3"})
    if normalized_config.template_key != "grabelan-catalog-v3":
        raise InvalidCatalogRenderConfigError("cover-capable renders require the v3 template")
    layout = resolve_catalog_render_layout(normalized_config)
    template = resolve_catalog_template(normalized_config.template_key, template_root=template_root)
    snapshot = session.get(CatalogSnapshot, catalog_snapshot_id)
    if snapshot is None:
        raise InvalidCatalogRenderJobError(f"CatalogSnapshot not found: {catalog_snapshot_id}")
    read_catalog_snapshot_data(session, snapshot.id)
    profile = get_active_catalog_brand_profile(session, profile_id=brand_profile_id)
    branding = resolve_catalog_branding(profile, storage_root=storage_root)
    theme = resolve_catalog_theme(theme_key, theme_version, branding,
        primary_color_override=primary_color_override, accent_color_override=accent_color_override)
    cover = resolve_catalog_cover(cover_choice, branding, session, storage_root=storage_root)
    version = renderer_version.strip()
    if not version:
        raise InvalidCatalogRenderJobError("renderer_version is required")
    payload = CatalogRenderJobPayloadV4(
        catalog_snapshot_id=snapshot.id, snapshot_content_hash=snapshot.content_hash.lower(),
        snapshot_schema_version=snapshot.schema_version,
        template_key=template.key, template_version=template.version,
        template_hash=hash_catalog_template(template), renderer_version=version,
        config=normalized_config, config_hash=hash_catalog_render_config(normalized_config),
        catalog_brand_profile_id=profile.id, branding_schema_version=branding.schema_version,
        branding_hash=hash_resolved_catalog_branding(branding), branding_data=branding,
        layout_key=layout.key, layout_version=layout.version,
        theme_schema_version=theme.schema_version, theme_hash=hash_resolved_catalog_theme(theme), theme_data=theme,
        cover_schema_version=cover.schema_version, cover_hash=hash_resolved_catalog_cover(cover), cover_data=cover,
    )
    return enqueue_job(
        session, job_type=CATALOG_RENDER_JOB_TYPE_V4,
        payload=payload.model_dump(mode="json"),
        idempotency_key=build_catalog_render_idempotency_key(payload, job_type=CATALOG_RENDER_JOB_TYPE_V4),
        max_attempts=max_attempts,
    )


def enqueue_catalog_render(
    session: Session,
    *,
    catalog_snapshot_id: uuid.UUID,
    config: CatalogRenderConfig | Mapping[str, Any] | None = None,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    renderer_version: str = CATALOG_RENDERER_VERSION,
    max_attempts: int = 3,
) -> Job:
    normalized_config = normalize_catalog_render_config(config)
    if normalized_config.layout != "classic":
        raise InvalidCatalogRenderConfigError(
            "legacy catalog.render.v1 only supports the classic layout"
        )
    template = resolve_catalog_template(
        normalized_config.template_key,
        template_root=template_root,
    )
    snapshot = session.get(CatalogSnapshot, catalog_snapshot_id)
    if snapshot is None:
        raise InvalidCatalogRenderJobError(
            f"CatalogSnapshot not found: {catalog_snapshot_id}"
        )
    try:
        read_catalog_snapshot_data(session, catalog_snapshot_id)
    except CatalogSnapshotError as error:
        raise InvalidCatalogRenderJobError(str(error)) from None
    normalized_renderer_version = renderer_version.strip()
    if not normalized_renderer_version:
        raise InvalidCatalogRenderJobError("renderer_version is required")
    payload = CatalogRenderJobPayload(
        catalog_snapshot_id=snapshot.id,
        snapshot_content_hash=snapshot.content_hash.lower(),
        snapshot_schema_version=snapshot.schema_version,
        template_key=template.key,
        template_version=template.version,
        template_hash=hash_catalog_template(template),
        renderer_version=normalized_renderer_version,
        config=normalized_config,
        config_hash=hash_catalog_render_config(normalized_config),
    )
    return enqueue_job(
        session,
        job_type=CATALOG_RENDER_JOB_TYPE,
        payload=payload.model_dump(mode="json"),
        idempotency_key=build_catalog_render_idempotency_key(payload),
        max_attempts=max_attempts,
    )


def create_running_catalog_render_run(
    session: Session,
    *,
    payload: CatalogRenderJobPayload | CatalogRenderJobPayloadV2 | CatalogRenderJobPayloadV3 | CatalogRenderJobPayloadV4,
    job_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
) -> tuple[CatalogRenderRun, CatalogSnapshotData]:
    snapshot = session.get(CatalogSnapshot, payload.catalog_snapshot_id)
    if snapshot is None:
        raise InvalidCatalogRenderJobError(
            f"CatalogSnapshot not found: {payload.catalog_snapshot_id}"
        )
    try:
        snapshot_data = read_catalog_snapshot_data(session, snapshot.id)
    except CatalogSnapshotError as error:
        raise InvalidCatalogRenderJobError(str(error)) from None
    if (
        snapshot.content_hash.lower() != payload.snapshot_content_hash.lower()
        or snapshot.schema_version != payload.snapshot_schema_version
        or payload.config_hash != hash_catalog_render_config(payload.config)
        or payload.template_key != payload.config.template_key
    ):
        raise InvalidCatalogRenderJobError(
            "catalog render Job lineage no longer matches its trusted inputs"
        )
    job = None
    if job_id is not None:
        job = session.get(Job, job_id)
        expected_type = CATALOG_RENDER_JOB_TYPE_V4 if isinstance(payload, CatalogRenderJobPayloadV4) else CATALOG_RENDER_JOB_TYPE_V3 if isinstance(payload, CatalogRenderJobPayloadV3) else CATALOG_RENDER_JOB_TYPE_V2 if isinstance(payload, CatalogRenderJobPayloadV2) else CATALOG_RENDER_JOB_TYPE
        if job is None or job.job_type != expected_type:
            raise InvalidCatalogRenderJobError(
                f"job must exist with type {expected_type}"
            )
    if isinstance(payload, CatalogRenderJobPayloadV2):
        if hash_resolved_catalog_branding(payload.branding_data) != payload.branding_hash.lower():
            raise InvalidCatalogRenderJobError("frozen branding payload hash mismatch")
        layout = resolve_catalog_render_layout(payload.config)
        if payload.layout_key != layout.key or payload.layout_version != layout.version:
            raise InvalidCatalogRenderJobError(
                "catalog render Job layout lineage is no longer supported"
            )
    if isinstance(payload, CatalogRenderJobPayloadV3):
        theme = payload.theme_data
        definition = resolve_catalog_theme_definition(theme.theme_key, theme.theme_version)
        if (theme.css_class != definition.css_class
            or hash_resolved_catalog_theme(theme) != payload.theme_hash.lower()):
            raise InvalidCatalogRenderJobError("frozen theme payload hash or registry lineage mismatch")
    if isinstance(payload, CatalogRenderJobPayloadV4):
        cover = payload.cover_data
        if cover.enabled:
            resolve_catalog_cover_definition(cover.cover_key or "", cover.cover_version or "")
        if hash_resolved_catalog_cover(cover) != payload.cover_hash.lower():
            raise InvalidCatalogRenderJobError("frozen cover payload hash mismatch")
    run = CatalogRenderRun(
        catalog_snapshot=snapshot,
        job=job,
        template_key=payload.template_key,
        template_version=payload.template_version,
        template_hash=payload.template_hash.lower(),
        renderer_version=payload.renderer_version,
        renderer_engine=CATALOG_RENDERER_ENGINE,
        renderer_engine_version=None,
        locale=payload.config.locale,
        config_hash=payload.config_hash.lower(),
        layout_key=(payload.layout_key if isinstance(payload, CatalogRenderJobPayloadV2) else None),
        layout_version=(payload.layout_version if isinstance(payload, CatalogRenderJobPayloadV2) else None),
        catalog_brand_profile_id=(payload.catalog_brand_profile_id if isinstance(payload, CatalogRenderJobPayloadV2) else None),
        branding_schema_version=(payload.branding_schema_version if isinstance(payload, CatalogRenderJobPayloadV2) else None),
        branding_hash=(payload.branding_hash.lower() if isinstance(payload, CatalogRenderJobPayloadV2) else None),
        branding_data=(payload.branding_data.model_dump(mode="json") if isinstance(payload, CatalogRenderJobPayloadV2) else None),
        theme_schema_version=(payload.theme_schema_version if isinstance(payload, CatalogRenderJobPayloadV3) else None),
        theme_hash=(payload.theme_hash.lower() if isinstance(payload, CatalogRenderJobPayloadV3) else None),
        theme_data=(payload.theme_data.model_dump(mode="json") if isinstance(payload, CatalogRenderJobPayloadV3) else None),
        cover_schema_version=(payload.cover_schema_version if isinstance(payload, CatalogRenderJobPayloadV4) else None),
        cover_hash=(payload.cover_hash.lower() if isinstance(payload, CatalogRenderJobPayloadV4) else None),
        cover_data=(payload.cover_data.model_dump(mode="json") if isinstance(payload, CatalogRenderJobPayloadV4) else None),
        status=ExtractionRunStatus.RUNNING,
        started_at=started_at or utc_now(),
    )
    session.add(run)
    session.flush()
    return run, snapshot_data


def mark_catalog_render_run_failed(
    session: Session,
    run: CatalogRenderRun,
    *,
    error: Exception | str,
    completed_at: datetime | None = None,
) -> CatalogRenderRun:
    _require_running(run)
    run.status = ExtractionRunStatus.FAILED
    run.sanitized_error = sanitize_extraction_error(error)
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def complete_catalog_render_run(
    session: Session,
    run: CatalogRenderRun,
    *,
    stored_pdf: StoredCatalogPdf,
    renderer_engine: str,
    renderer_engine_version: str | None,
    completed_at: datetime | None = None,
) -> CatalogArtifact:
    _require_running(run)
    if run.artifact is not None:
        raise CompletedCatalogRenderRunError(
            f"CatalogRenderRun already has an artifact: {run.id}"
        )
    engine = renderer_engine.strip()
    engine_version = (
        renderer_engine_version.strip() if renderer_engine_version else None
    )
    if not engine:
        raise CatalogRenderError("renderer engine is required")
    artifact = CatalogArtifact(
        catalog_snapshot=run.catalog_snapshot,
        render_run=run,
        media_type=PDF_MEDIA_TYPE,
        file_path=stored_pdf.file_path,
        checksum_sha256=stored_pdf.checksum_sha256,
        file_size_bytes=stored_pdf.file_size_bytes,
        page_count=stored_pdf.page_count,
    )
    session.add(artifact)
    run.status = ExtractionRunStatus.SUCCEEDED
    run.renderer_engine = engine
    run.renderer_engine_version = engine_version
    run.sanitized_error = None
    run.completed_at = completed_at or utc_now()
    session.flush()
    return artifact


def build_catalog_render_view_model(
    snapshot: CatalogSnapshotData,
    config: CatalogRenderConfig,
    *,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
    store_name: str = "Grabelan",
    branding: ResolvedCatalogBranding | None = None,
    theme: ResolvedCatalogTheme | None = None,
    cover: ResolvedCatalogCover | None = None,
) -> CatalogRenderViewModel:
    layout = resolve_catalog_render_layout(config, require_registered_geometry=False)
    babel_locale = config.locale.replace("-", "_")
    sections: list[CatalogRenderSectionView] = []
    for section in snapshot.sections:
        products: list[CatalogRenderProductView] = []
        for product in section.products:
            content = load_verified_snapshot_presentation_asset(
                product.hero.presentation_asset,
                storage_root=storage_root,
            )
            variants = [
                CatalogRenderVariantView(
                    source_sku_id=variant.source_sku_id,
                    label=build_variant_label(variant, locale=babel_locale),
                    price_display=format_currency(
                        variant.price.amount,
                        snapshot.currency,
                        locale=babel_locale,
                    ),
                )
                for variant in product.variants
            ]
            products.append(
                CatalogRenderProductView(
                    source_product_id=product.source_product_id,
                    brand_name=product.brand_name,
                    product_name=product.product_name,
                    short_description=product.short_description,
                    image_data_uri=(
                        f"data:{product.hero.presentation_asset.mime_type};base64,"
                        + base64.b64encode(content).decode("ascii")
                    ),
                    variants=variants,
                )
            )
        sections.append(
            CatalogRenderSectionView(
                source_category_id=section.category.source_category_id,
                category_name=section.category.name,
                products=products,
            )
        )
    return CatalogRenderViewModel(
        locale=config.locale,
        page_size=config.page_size,
        orientation=config.orientation,
        store_name=branding.display_name if branding else store_name,
        branding=build_catalog_branding_view(branding, storage_root=storage_root) if branding else None,
        theme=theme,
        cover=cover,
        cover_hero_data_uri=(f"data:{cover.hero.mime_type};base64," + base64.b64encode(
            load_frozen_catalog_cover_hero(cover.hero, storage_root=storage_root)
        ).decode("ascii")) if cover and cover.hero else None,
        layout=CatalogRenderLayoutView(
            key=layout.key,
            version=layout.version,
            products_per_row=layout.products_per_row,
            page_size=layout.page_size,
            orientation=layout.orientation,
            css_class=layout.css_class,
        ),
        title="Catálogo",
        as_of_label=format_date(
            snapshot.as_of.date(), format="long", locale=babel_locale
        ),
        currency=snapshot.currency,
        sections=sections,
    )


def build_catalog_branding_view(
    branding: ResolvedCatalogBranding,
    *,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> CatalogBrandingView:
    logo_data_uri = None
    if branding.logo is not None:
        content = load_frozen_catalog_brand_logo(branding.logo, storage_root=storage_root)
        logo_data_uri = f"data:{branding.logo.mime_type};base64," + base64.b64encode(content).decode("ascii")
    return CatalogBrandingView(
        display_name=branding.display_name, logo_data_uri=logo_data_uri,
        primary_color=branding.primary_color, accent_color=branding.accent_color,
        contact_text=branding.contact_text, social_handle=branding.social_handle,
    )


def build_variant_label(
    variant: CatalogVariantSnapshot,
    *,
    locale: str,
) -> str | None:
    pieces: list[str] = []
    if variant.flavor:
        pieces.append(variant.flavor)
    if variant.size_value is not None and variant.size_unit is not None:
        size = format_decimal(
            variant.size_value,
            locale=locale,
            decimal_quantization=False,
            group_separator=False,
        )
        pieces.append(f"{size} {variant.size_unit}")
    return " / ".join(pieces) or None


def load_verified_snapshot_presentation_asset(
    asset: FrozenCatalogAsset,
    *,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> bytes:
    root = storage_root.resolve()
    path = (root / asset.storage_relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise SnapshotAssetIntegrityError(
            "frozen presentation asset path is outside canonical storage"
        ) from None
    if path.relative_to(root).as_posix() != asset.storage_relative_path:
        raise SnapshotAssetIntegrityError(
            "frozen presentation asset path is not canonical"
        )
    try:
        content = path.read_bytes()
    except OSError:
        raise SnapshotAssetIntegrityError(
            "frozen presentation asset is unavailable"
        ) from None
    if hashlib.sha256(content).hexdigest() != asset.checksum_sha256.lower():
        raise SnapshotAssetIntegrityError(
            "frozen presentation asset failed checksum verification"
        )
    try:
        mime_type, extension, width, height = inspect_supported_image(content)
    except PhotoIntakeError:
        raise SnapshotAssetIntegrityError(
            "frozen presentation asset is corrupt or unsupported"
        ) from None
    if (
        mime_type != asset.mime_type
        or path.suffix.lower() != extension
        or len(content) != asset.file_size_bytes
        or width != asset.width
        or height != asset.height
    ):
        raise SnapshotAssetIntegrityError(
            "frozen presentation asset metadata failed verification"
        )
    return content


def render_catalog_html(
    view_model: CatalogRenderViewModel,
    template: CatalogTemplate,
) -> str:
    try:
        source = template.html_path.read_text(encoding="utf-8")
        stylesheet = template.css_path.read_text(encoding="utf-8")
        if template.theme_css_path is not None:
            stylesheet += "\n" + template.theme_css_path.read_text(encoding="utf-8")
        if template.cover_css_path is not None:
            stylesheet += "\n" + template.cover_css_path.read_text(encoding="utf-8")
    except OSError:
        raise InvalidCatalogTemplateError(
            "catalog template could not be read"
        ) from None
    if _contains_external_dependency(source) or _contains_external_dependency(
        stylesheet
    ):
        raise InvalidCatalogTemplateError(
            "catalog template contains an external or filesystem dependency"
        )
    environment = Environment(
        autoescape=select_autoescape(default_for_string=True, default=True),
        undefined=StrictUndefined,
    )
    try:
        html = environment.from_string(source).render(
            **view_model.model_dump(mode="python"),
            stylesheet=Markup(stylesheet),
        )
    except Exception:
        raise InvalidCatalogTemplateError("catalog template rendering failed") from None
    if _contains_external_dependency(html):
        raise InvalidCatalogTemplateError(
            "rendered catalog contains an external or filesystem dependency"
        )
    return html


def validate_catalog_pdf(pdf_bytes: bytes) -> int:
    if not pdf_bytes.startswith(b"%PDF-"):
        raise InvalidCatalogPdfError("renderer returned invalid PDF data")
    try:
        page_count = len(PdfReader(BytesIO(pdf_bytes)).pages)
    except Exception:
        raise InvalidCatalogPdfError("renderer returned an unreadable PDF") from None
    if page_count < 1:
        raise InvalidCatalogPdfError("renderer returned a PDF with no pages")
    return page_count


def store_catalog_pdf(
    pdf_bytes: bytes,
    *,
    catalogs_dir: Path = DEFAULT_CATALOGS_DIR,
) -> StoredCatalogPdf:
    page_count = validate_catalog_pdf(pdf_bytes)
    checksum = hashlib.sha256(pdf_bytes).hexdigest()
    root = catalogs_dir.resolve()
    destination = root / checksum[:2] / f"{checksum}.pdf"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = destination.read_bytes()
            if (
                hashlib.sha256(existing).hexdigest() != checksum
                or existing != pdf_bytes
            ):
                raise CatalogPdfStorageError(
                    "existing catalog PDF does not match its content identity"
                )
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=".catalog-render-",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(pdf_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)
    except CatalogPdfStorageError:
        raise
    except OSError:
        raise CatalogPdfStorageError("catalog PDF storage failed") from None
    return StoredCatalogPdf(
        file_path=str(destination),
        checksum_sha256=checksum,
        file_size_bytes=len(pdf_bytes),
        page_count=page_count,
    )


def _contains_external_dependency(content: str) -> bool:
    lowered = content.lower()
    return any(
        marker in lowered
        for marker in ("http://", "https://", "file://", "url(")
    )


def _require_running(run: CatalogRenderRun) -> None:
    if run.status is not ExtractionRunStatus.RUNNING:
        raise CompletedCatalogRenderRunError(
            f"CatalogRenderRun is already complete: {run.id}"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
