"""Immutable CatalogBuild history projected from existing frozen records."""

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import CatalogArtifact, CatalogBuild, CatalogRenderRun, Job
from app.domain.catalog_history import (
    CatalogHistoryDetail, CatalogHistoryItem, CatalogHistoryOptions,
    CatalogHistoryPage, HistoryAttempt, HistoryContact, HistoryFeature, HistoryProduct,
    HistoryPublisher, HistoryVariant,
)
from app.domain.enums import ExtractionRunStatus, JobStatus
from app.domain.schemas import (
    CatalogBuildArtifactSummary, CatalogRenderJobPayloadV2,
    CatalogRenderJobPayloadV3, CatalogRenderJobPayloadV4,
    CatalogRenderJobPayloadV5, CatalogSnapshotData,
)
from app.rendering.catalog_closings import UnknownCatalogClosingError, resolve_catalog_closing_definition
from app.rendering.catalog_covers import UnknownCatalogCoverError, resolve_catalog_cover_definition
from app.rendering.catalog_themes import UnknownCatalogThemeError, resolve_catalog_theme_definition
from app.services.catalog_builds import (
    CatalogBuildArtifactIntegrityError, UnknownCatalogBuildArtifactError,
    _public_render_error, resolve_catalog_build_artifact_pdf,
)
from app.services.catalog_rendering import DEFAULT_CATALOGS_DIR
from app.services.catalog_snapshots import CatalogSnapshotError, read_catalog_snapshot_data

HistoryFilter = Literal["all", "ready", "active", "failed"]
DateFilter = Literal["all", "today", "7d", "30d"]
_PAYLOAD_TYPES = {
    "catalog.render.v2": CatalogRenderJobPayloadV2,
    "catalog.render.v3": CatalogRenderJobPayloadV3,
    "catalog.render.v4": CatalogRenderJobPayloadV4,
    "catalog.render.v5": CatalogRenderJobPayloadV5,
}
_LAYOUT_NAMES = {"classic": "Classic", "dense": "Dense", "compact": "Compact"}


class UnknownHistoryBuildError(ValueError):
    pass


def _history_query_conditions(
    *, search: str, status: HistoryFilter, publisher: str | None,
    theme: str | None, period: DateFilter, now: datetime,
) -> list:
    conditions = []
    if status == "ready":
        conditions.append(Job.status == JobStatus.SUCCEEDED)
    elif status == "active":
        conditions.append(Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
    elif status == "failed":
        conditions.append(Job.status == JobStatus.FAILED)
    frozen_publisher = func.json_extract(Job.payload, "$.branding_data.display_name")
    if publisher:
        conditions.append(frozen_publisher == publisher)
    if theme:
        conditions.append(func.json_extract(Job.payload, "$.theme_data.theme_key") == theme)
    if search:
        needle = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if needle:
            pattern = f"%{needle}%"
            conditions.append(or_(
                frozen_publisher.ilike(pattern, escape="\\"),
                func.json_extract(Job.payload, "$.cover_data.title").ilike(pattern, escape="\\"),
                func.json_extract(Job.payload, "$.cover_data.edition_label").ilike(pattern, escape="\\"),
            ))
    if period == "today":
        conditions.append(CatalogBuild.created_at >= now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
    elif period != "all":
        conditions.append(CatalogBuild.created_at >= now - timedelta(days=int(period[:-1])))
    return conditions


def _runs_by_job(session: Session, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[CatalogRenderRun]]:
    if not job_ids:
        return {}
    runs = session.scalars(
        select(CatalogRenderRun)
        .where(CatalogRenderRun.job_id.in_(job_ids))
        .options(
            selectinload(CatalogRenderRun.artifact).selectinload(CatalogArtifact.render_run),
            selectinload(CatalogRenderRun.job).selectinload(Job.catalog_build),
        )
        .order_by(CatalogRenderRun.created_at.desc(), CatalogRenderRun.id.desc())
    ).all()
    grouped: dict[uuid.UUID, list[CatalogRenderRun]] = defaultdict(list)
    for run in runs:
        if run.job_id is not None:
            grouped[run.job_id].append(run)
    return grouped


def _payload(job: Job):
    schema = _PAYLOAD_TYPES.get(job.job_type)
    if schema is None:
        return None
    try:
        return schema.model_validate(job.payload)
    except ValidationError:
        return None


def _snapshot(session: Session, build: CatalogBuild) -> CatalogSnapshotData | None:
    try:
        return read_catalog_snapshot_data(session, build.catalog_snapshot_id)
    except CatalogSnapshotError:
        return None


def _definition_label(resolver, key: str, version: str) -> str:
    try:
        return resolver(key, version).display_name
    except (UnknownCatalogThemeError, UnknownCatalogCoverError, UnknownCatalogClosingError):
        return f"{key} v{version}"


def _feature(payload, *, kind: Literal["cover", "closing"]) -> HistoryFeature:
    if payload is None:
        return HistoryFeature(state="invalid")
    supported = isinstance(payload, CatalogRenderJobPayloadV4 if kind == "cover" else CatalogRenderJobPayloadV5)
    if not supported:
        return HistoryFeature(state="unavailable")
    data = payload.cover_data if kind == "cover" else payload.closing_data
    if not data.enabled:
        return HistoryFeature(state="disabled")
    key = data.cover_key if kind == "cover" else data.closing_key
    version = data.cover_version if kind == "cover" else data.closing_version
    resolver = resolve_catalog_cover_definition if kind == "cover" else resolve_catalog_closing_definition
    return HistoryFeature(
        state="enabled", key=key, version=version,
        display_name=_definition_label(resolver, key, version),
        title=data.title if kind == "cover" else None,
        subtitle=data.subtitle if kind == "cover" else None,
        edition_label=data.edition_label if kind == "cover" else None,
        heading=data.heading if kind == "closing" else None,
        note=data.note if kind == "closing" else None,
        show_publisher_logo=data.show_publisher_logo,
        hero_present=data.hero is not None if kind == "cover" else False,
        contacts=[HistoryContact(kind=item.kind, value=item.value, href=item.href) for item in data.contacts] if kind == "closing" else [],
        qr_target_type=data.qr_target_type if kind == "closing" and data.qr_enabled else None,
        qr_target_url=data.qr_target_url if kind == "closing" and data.qr_enabled else None,
    )


def _available_artifact(
    session: Session, job: Job, runs: list[CatalogRenderRun], catalogs_dir: Path,
) -> CatalogBuildArtifactSummary | None:
    if job.status is not JobStatus.SUCCEEDED:
        return None
    successful = next((run for run in runs if run.status is ExtractionRunStatus.SUCCEEDED and run.artifact is not None), None)
    if successful is None:
        return None
    artifact = successful.artifact
    try:
        resolve_catalog_build_artifact_pdf(session, artifact.id, catalogs_dir=catalogs_dir)
    except (UnknownCatalogBuildArtifactError, CatalogBuildArtifactIntegrityError):
        return None
    return CatalogBuildArtifactSummary(
        id=artifact.id, created_at=artifact.created_at, page_count=artifact.page_count,
        preview_url=f"/api/catalog-builder/artifacts/{artifact.id}/pdf",
        download_url=f"/api/catalog-builder/artifacts/{artifact.id}/pdf?download=true",
    )


def _project(
    session: Session, build: CatalogBuild, runs: list[CatalogRenderRun], catalogs_dir: Path,
) -> tuple[CatalogHistoryItem, CatalogSnapshotData | None]:
    job = build.job
    payload = _payload(job)
    if payload is not None and (
        build.catalog_snapshot is None
        or payload.catalog_snapshot_id != build.catalog_snapshot_id
        or payload.snapshot_content_hash.lower() != build.catalog_snapshot.content_hash.lower()
    ):
        payload = None
    snapshot = _snapshot(session, build)
    product_count = sum(len(section.products) for section in snapshot.sections) if snapshot else None
    sku_count = sum(len(product.variants) for section in snapshot.sections for product in section.products) if snapshot else None
    publisher = None
    if payload is not None:
        branding = payload.branding_data
        publisher = HistoryPublisher(
            key=branding.profile_key, display_name=branding.display_name,
            primary_color=branding.primary_color, accent_color=branding.accent_color,
            contact_text=branding.contact_text, social_handle=branding.social_handle,
            logo_present=branding.logo is not None,
        )
    theme = payload.theme_data if isinstance(payload, CatalogRenderJobPayloadV3) else None
    artifact = _available_artifact(session, job, runs, catalogs_dir)
    latest = runs[0] if runs else None
    item = CatalogHistoryItem(
        build_id=build.id, status=job.status.value, created_at=build.created_at,
        completed_at=job.finished_at, render_version=job.job_type,
        publisher=publisher, product_count=product_count, sku_count=sku_count,
        layout_key=payload.layout_key if payload else None,
        layout_version=payload.layout_version if payload else None,
        layout_display_name=(_LAYOUT_NAMES.get(payload.layout_key) if payload.layout_version == "1" else None) or f"{payload.layout_key} v{payload.layout_version}" if payload else None,
        theme_key=theme.theme_key if theme else None,
        theme_version=theme.theme_version if theme else None,
        theme_display_name=_definition_label(resolve_catalog_theme_definition, theme.theme_key, theme.theme_version) if theme else None,
        palette_source=theme.palette_source if theme else None,
        primary_color=theme.primary_color if theme else None,
        accent_color=theme.accent_color if theme else None,
        cover=_feature(payload, kind="cover"), closing=_feature(payload, kind="closing"),
        page_count=artifact.page_count if artifact else None,
        artifact_available=artifact is not None, artifact=artifact,
        latest_render_status=latest.status.value if latest else None,
        error=_public_render_error(latest.sanitized_error if latest else job.last_error) if job.status is JobStatus.FAILED else None,
        historical_data_available=payload is not None and snapshot is not None,
    )
    return item, snapshot


def list_catalog_history(
    session: Session, *, page: int = 1, page_size: int = 20, search: str = "",
    status: HistoryFilter = "all", publisher: str | None = None,
    theme: str | None = None, period: DateFilter = "all",
    now: datetime | None = None, catalogs_dir: Path | None = None,
) -> CatalogHistoryPage:
    conditions = _history_query_conditions(
        search=search, status=status, publisher=publisher, theme=theme,
        period=period, now=now or datetime.now(timezone.utc),
    )
    total = session.scalar(select(func.count(CatalogBuild.id)).join(CatalogBuild.job).where(*conditions)) or 0
    builds = session.scalars(
        select(CatalogBuild).join(CatalogBuild.job).where(*conditions)
        .options(joinedload(CatalogBuild.job), joinedload(CatalogBuild.catalog_snapshot))
        .order_by(CatalogBuild.created_at.desc(), CatalogBuild.id.desc())
        .limit(page_size).offset((page - 1) * page_size)
    ).all()
    runs = _runs_by_job(session, [build.job_id for build in builds])
    root = catalogs_dir or DEFAULT_CATALOGS_DIR
    return CatalogHistoryPage(
        items=[_project(session, build, runs.get(build.job_id, []), root)[0] for build in builds],
        page=page, page_size=page_size, total=total,
        all_total=session.scalar(select(func.count(CatalogBuild.id))) or 0,
    )


def get_catalog_history_detail(
    session: Session, build_id: uuid.UUID, *, catalogs_dir: Path | None = None,
) -> CatalogHistoryDetail:
    build = session.scalar(
        select(CatalogBuild).where(CatalogBuild.id == build_id)
        .options(joinedload(CatalogBuild.job), joinedload(CatalogBuild.catalog_snapshot))
    )
    if build is None:
        raise UnknownHistoryBuildError("Catalog not found")
    runs = _runs_by_job(session, [build.job_id]).get(build.job_id, [])
    summary, snapshot = _project(session, build, runs, catalogs_dir or DEFAULT_CATALOGS_DIR)
    products = []
    if snapshot is not None:
        for section in snapshot.sections:
            for product in section.products:
                products.append(HistoryProduct(
                    category_name=section.category.name,
                    brand_name=product.brand_name, product_name=product.product_name,
                    short_description=product.short_description,
                    variants=[HistoryVariant(
                        external_sku=variant.external_sku, flavor=variant.flavor,
                        size_value=variant.model_dump(mode="json")["size_value"],
                        size_unit=variant.size_unit, servings=variant.servings,
                        price_amount=variant.price.model_dump(mode="json")["amount"],
                        price_currency=variant.price.currency,
                    ) for variant in product.variants],
                ))
    attempts = [HistoryAttempt(
        attempt=len(runs) - index, status=run.status.value,
        started_at=run.started_at, completed_at=run.completed_at,
        page_count=run.artifact.page_count if run.artifact else None,
        error=_public_render_error(run.sanitized_error) if run.status is ExtractionRunStatus.FAILED else None,
        render_version=build.job.job_type,
    ) for index, run in enumerate(runs)]
    source_build = session.get(CatalogBuild, build.source_build_id) if build.source_build_id else None
    source_payload = _payload(build.job)
    if source_payload is not None and (
        build.catalog_snapshot is None
        or source_payload.catalog_snapshot_id != build.catalog_snapshot_id
        or source_payload.snapshot_content_hash.lower() != build.catalog_snapshot.content_hash.lower()
    ):
        source_payload = None
    return CatalogHistoryDetail(
        **summary.model_dump(),
        can_duplicate=summary.historical_data_available,
        duplicate_unavailable_reason=(
            "unsupported_configuration" if source_payload is None else "invalid_snapshot"
        ) if not summary.historical_data_available else None,
        source_build_id=build.source_build_id,
        source_build_created_at=source_build.created_at if source_build else None,
        snapshot_schema_version=snapshot.schema_version if snapshot else None,
        currency=snapshot.currency if snapshot else None,
        as_of=snapshot.as_of if snapshot else None,
        products=products, render_attempts=attempts,
    )


def catalog_history_options(session: Session) -> CatalogHistoryOptions:
    publisher = func.json_extract(Job.payload, "$.branding_data.display_name")
    theme = func.json_extract(Job.payload, "$.theme_data.theme_key")
    publishers = session.scalars(
        select(publisher).join(CatalogBuild, CatalogBuild.job_id == Job.id)
        .where(publisher.is_not(None)).distinct().order_by(publisher)
    ).all()
    themes = session.scalars(
        select(theme).join(CatalogBuild, CatalogBuild.job_id == Job.id)
        .where(theme.is_not(None)).distinct().order_by(theme)
    ).all()
    return CatalogHistoryOptions(publishers=publishers, themes=themes)
