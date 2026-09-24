"""Thin, transactional orchestration for Catalog Builder create actions."""

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogArtifact, CatalogBuild, CatalogRenderRun
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus, JobStatus
from app.domain.schemas import (
    CatalogBuildArtifactSummary,
    CatalogBuildCreate,
    CatalogBuildRead,
    CatalogRenderConfig,
    CatalogRenderJobPayloadV2,
    CatalogRenderJobPayloadV3,
    CatalogRenderJobPayloadV4,
    CatalogRenderJobPayloadV5,
    CatalogCoverCreate,
)
from app.rendering.catalog_covers import resolve_catalog_cover_definition
from app.rendering.catalog_closings import resolve_catalog_closing_definition
from app.rendering.catalog_themes import resolve_catalog_theme_definition
from app.rendering.catalog_layouts import (
    UnknownCatalogLayoutError,
    resolve_catalog_layout,
)
from app.services.catalog_rendering import (
    DEFAULT_CATALOGS_DIR,
    DEFAULT_STORAGE_ROOT,
    DEFAULT_TEMPLATE_ROOT,
    CATALOG_RENDER_JOB_TYPE_V2,
    CATALOG_RENDER_JOB_TYPE_V3,
    CATALOG_RENDER_JOB_TYPE_V4,
    CATALOG_RENDER_JOB_TYPE_V5,
    enqueue_catalog_render_v2,
    enqueue_catalog_render_v3,
    enqueue_catalog_render_v4,
    enqueue_catalog_render_v5,
    validate_catalog_pdf,
)
from app.services.catalog_snapshots import (
    create_catalog_snapshot,
    read_catalog_snapshot_data,
)
from app.services.jobs import JobRecoveryError, requeue_failed_job

Clock = Callable[[], datetime]

_LAYOUT_LABELS = {
    "classic": "Classic",
    "dense": "Dense",
    "compact": "Compact",
}


class CatalogBuildError(ValueError):
    pass


class UnknownCatalogBuildError(CatalogBuildError):
    pass


class CatalogBuildIdempotencyConflictError(CatalogBuildError):
    pass


class CatalogBuildRetryError(CatalogBuildError):
    pass


class CatalogBuildIntegrityError(CatalogBuildError):
    pass


class UnknownCatalogBuildArtifactError(CatalogBuildError):
    pass


class CatalogBuildArtifactIntegrityError(CatalogBuildError):
    pass


def create_catalog_build(
    session: Session,
    request: CatalogBuildCreate,
    *,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    clock: Clock = utc_now,
) -> CatalogBuildRead:
    """Create snapshot + versioned render Job + build owner in one DB transaction.

    The caller commits only after this returns. SQLite's immediate write lock
    serializes competing create actions before the idempotency-key lookup.
    """

    _begin_catalog_build_transaction(session)
    try:
        request_hash = hash_catalog_build_request(request)
        existing = _find_by_idempotency_key(session, request.idempotency_key)
        if existing is not None:
            _require_matching_request(existing, request_hash)
            return get_catalog_build(session, existing.id)

        layout = resolve_catalog_layout(request.layout_key)
        if request.layout_version != layout.version:
            raise UnknownCatalogLayoutError(
                f"unsupported catalog layout version: {request.layout_key}/{request.layout_version}"
            )

        snapshot = create_catalog_snapshot(
            session,
            {
                "product_ids": request.product_ids,
                "currency": request.currency,
            },
            storage_root=storage_root,
            clock=clock,
        )
        if request.closing is not None:
            job = enqueue_catalog_render_v5(
                session, catalog_snapshot_id=snapshot.id,
                brand_profile_id=request.catalog_brand_profile_id,
                theme_key=request.theme_key, theme_version=request.theme_version,
                primary_color_override=request.primary_color_override,
                accent_color_override=request.accent_color_override,
                cover_choice=request.cover or CatalogCoverCreate(enabled=False),
                closing_choice=request.closing,
                config=CatalogRenderConfig(layout=layout.key, template_key="grabelan-catalog-v4"),
                storage_root=storage_root, template_root=template_root,
            )
        elif request.theme_key is None:
            job = enqueue_catalog_render_v2(
                session, catalog_snapshot_id=snapshot.id,
                brand_profile_id=request.catalog_brand_profile_id,
                config=CatalogRenderConfig(layout=layout.key),
                storage_root=storage_root, template_root=template_root,
            )
        elif request.cover is None:
            job = enqueue_catalog_render_v3(
                session, catalog_snapshot_id=snapshot.id,
                brand_profile_id=request.catalog_brand_profile_id,
                theme_key=request.theme_key, theme_version=request.theme_version,
                primary_color_override=request.primary_color_override,
                accent_color_override=request.accent_color_override,
                config=CatalogRenderConfig(layout=layout.key, template_key="grabelan-catalog-v2"),
                storage_root=storage_root, template_root=template_root,
            )
        else:
            job = enqueue_catalog_render_v4(
                session, catalog_snapshot_id=snapshot.id,
                brand_profile_id=request.catalog_brand_profile_id,
                theme_key=request.theme_key, theme_version=request.theme_version,
                primary_color_override=request.primary_color_override,
                accent_color_override=request.accent_color_override,
                cover_choice=request.cover,
                config=CatalogRenderConfig(layout=layout.key, template_key="grabelan-catalog-v3"),
                storage_root=storage_root, template_root=template_root,
            )
        build = CatalogBuild(
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            catalog_snapshot=snapshot,
            job=job,
        )
        session.add(build)
        session.flush()
        return get_catalog_build(session, build.id)
    except Exception:
        session.rollback()
        raise


def _begin_catalog_build_transaction(session: Session) -> None:
    if session.in_transaction():
        raise CatalogBuildError("Catalog build creation requires a fresh Session transaction")
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def get_catalog_build(session: Session, build_id: uuid.UUID) -> CatalogBuildRead:
    build = session.get(CatalogBuild, build_id)
    if build is None:
        raise UnknownCatalogBuildError(f"Catalog build not found: {build_id}")
    job = build.job
    if job is None or job.job_type not in (CATALOG_RENDER_JOB_TYPE_V2, CATALOG_RENDER_JOB_TYPE_V3, CATALOG_RENDER_JOB_TYPE_V4, CATALOG_RENDER_JOB_TYPE_V5):
        raise CatalogBuildIntegrityError("Catalog build render Job is invalid")
    try:
        payload = (CatalogRenderJobPayloadV5 if job.job_type == CATALOG_RENDER_JOB_TYPE_V5 else CatalogRenderJobPayloadV4 if job.job_type == CATALOG_RENDER_JOB_TYPE_V4 else CatalogRenderJobPayloadV3 if job.job_type == CATALOG_RENDER_JOB_TYPE_V3 else CatalogRenderJobPayloadV2).model_validate(job.payload)
    except ValidationError:
        raise CatalogBuildIntegrityError("Catalog build render configuration is invalid") from None
    if payload.catalog_snapshot_id != build.catalog_snapshot_id:
        raise CatalogBuildIntegrityError("Catalog build snapshot lineage is invalid")

    snapshot = read_catalog_snapshot_data(session, build.catalog_snapshot_id)
    product_count = sum(len(section.products) for section in snapshot.sections)
    artifact = _successful_artifact_for_job(session, job.id)
    status = _normalized_status(job.status)
    if status == "succeeded" and artifact is None:
        raise CatalogBuildIntegrityError("Successful Catalog build has no PDF artifact")

    public_artifact = (
        CatalogBuildArtifactSummary(
            id=artifact.id,
            created_at=artifact.created_at,
            page_count=artifact.page_count,
            preview_url=f"/api/catalog-builder/artifacts/{artifact.id}/pdf",
            download_url=f"/api/catalog-builder/artifacts/{artifact.id}/pdf?download=true",
        )
        if artifact is not None
        else None
    )
    latest_run = _latest_render_run(session, job.id)
    return CatalogBuildRead(
        id=build.id,
        status=status,
        catalog_snapshot_id=build.catalog_snapshot_id,
        product_count=product_count,
        currency=snapshot.currency,
        catalog_brand_profile_id=payload.catalog_brand_profile_id,
        catalog_brand_key=payload.branding_data.profile_key,
        catalog_brand_display_name=payload.branding_data.display_name,
        layout_key=payload.layout_key,
        layout_version=payload.layout_version,
        layout_display_label=_LAYOUT_LABELS[payload.layout_key],
        theme_key=payload.theme_data.theme_key if isinstance(payload, CatalogRenderJobPayloadV3) else None,
        theme_version=payload.theme_data.theme_version if isinstance(payload, CatalogRenderJobPayloadV3) else None,
        theme_display_label=resolve_catalog_theme_definition(payload.theme_data.theme_key, payload.theme_data.theme_version).display_name if isinstance(payload, CatalogRenderJobPayloadV3) else "Legacy",
        palette_source=payload.theme_data.palette_source if isinstance(payload, CatalogRenderJobPayloadV3) else "legacy",
        primary_color=payload.theme_data.primary_color if isinstance(payload, CatalogRenderJobPayloadV3) else None,
        accent_color=payload.theme_data.accent_color if isinstance(payload, CatalogRenderJobPayloadV3) else None,
        cover_enabled=payload.cover_data.enabled if isinstance(payload, CatalogRenderJobPayloadV4) else False,
        cover_key=payload.cover_data.cover_key if isinstance(payload, CatalogRenderJobPayloadV4) else None,
        cover_version=payload.cover_data.cover_version if isinstance(payload, CatalogRenderJobPayloadV4) else None,
        cover_display_label=(resolve_catalog_cover_definition(payload.cover_data.cover_key, payload.cover_data.cover_version).display_name
            if isinstance(payload, CatalogRenderJobPayloadV4) and payload.cover_data.enabled else "None"),
        cover_title=payload.cover_data.title if isinstance(payload, CatalogRenderJobPayloadV4) else None,
        cover_subtitle=payload.cover_data.subtitle if isinstance(payload, CatalogRenderJobPayloadV4) else None,
        cover_edition_label=payload.cover_data.edition_label if isinstance(payload, CatalogRenderJobPayloadV4) else None,
        cover_show_publisher_logo=payload.cover_data.show_publisher_logo if isinstance(payload, CatalogRenderJobPayloadV4) else False,
        cover_hero_present=payload.cover_data.hero is not None if isinstance(payload, CatalogRenderJobPayloadV4) else False,
        closing_enabled=payload.closing_data.enabled if isinstance(payload, CatalogRenderJobPayloadV5) else False,
        closing_key=payload.closing_data.closing_key if isinstance(payload, CatalogRenderJobPayloadV5) else None,
        closing_version=payload.closing_data.closing_version if isinstance(payload, CatalogRenderJobPayloadV5) else None,
        closing_display_label=(resolve_catalog_closing_definition(payload.closing_data.closing_key, payload.closing_data.closing_version).display_name
            if isinstance(payload, CatalogRenderJobPayloadV5) and payload.closing_data.enabled else "None"),
        closing_heading=payload.closing_data.heading if isinstance(payload, CatalogRenderJobPayloadV5) else None,
        closing_note=payload.closing_data.note if isinstance(payload, CatalogRenderJobPayloadV5) else None,
        closing_show_publisher_logo=payload.closing_data.show_publisher_logo if isinstance(payload, CatalogRenderJobPayloadV5) else False,
        closing_contacts=payload.closing_data.contacts if isinstance(payload, CatalogRenderJobPayloadV5) else [],
        closing_qr_enabled=payload.closing_data.qr_enabled if isinstance(payload, CatalogRenderJobPayloadV5) else False,
        closing_qr_target_type=payload.closing_data.qr_target_type if isinstance(payload, CatalogRenderJobPayloadV5) else None,
        created_at=build.created_at,
        error=(
            _public_render_error(latest_run.sanitized_error if latest_run else None)
            if status == "failed"
            else None
        ),
        can_retry=(
            status == "failed" and job.attempts < job.max_attempts
        ),
        artifact=public_artifact,
    )


def retry_catalog_build(session: Session, build_id: uuid.UUID) -> CatalogBuildRead:
    build = session.get(CatalogBuild, build_id)
    if build is None:
        raise UnknownCatalogBuildError(f"Catalog build not found: {build_id}")
    try:
        requeue_failed_job(session, build.job)
    except JobRecoveryError as error:
        raise CatalogBuildRetryError(str(error)) from None
    return get_catalog_build(session, build.id)


def resolve_catalog_build_artifact_pdf(
    session: Session,
    artifact_id: uuid.UUID,
    *,
    catalogs_dir: Path = DEFAULT_CATALOGS_DIR,
) -> tuple[Path, CatalogArtifact]:
    artifact = session.get(CatalogArtifact, artifact_id)
    if artifact is None:
        raise UnknownCatalogBuildArtifactError(f"Catalog artifact not found: {artifact_id}")
    run = artifact.render_run
    if (
        run is None
        or run.status is not ExtractionRunStatus.SUCCEEDED
        or run.job_id is None
        or run.job is None
        or run.job.job_type not in (CATALOG_RENDER_JOB_TYPE_V2, CATALOG_RENDER_JOB_TYPE_V3, CATALOG_RENDER_JOB_TYPE_V4, CATALOG_RENDER_JOB_TYPE_V5)
        or run.job.catalog_build is None
        or run.job.catalog_build.catalog_snapshot_id != artifact.catalog_snapshot_id
        or artifact.catalog_snapshot_id != run.catalog_snapshot_id
    ):
        raise UnknownCatalogBuildArtifactError(
            f"Catalog Builder artifact not found: {artifact_id}"
        )

    root = catalogs_dir.resolve()
    expected = root / artifact.checksum_sha256[:2].lower() / (
        artifact.checksum_sha256.lower() + ".pdf"
    )
    path = Path(artifact.file_path).resolve()
    if path != expected or not path.is_relative_to(root):
        raise CatalogBuildArtifactIntegrityError("Catalog PDF locator is invalid")
    try:
        content = path.read_bytes()
    except OSError:
        raise CatalogBuildArtifactIntegrityError("Catalog PDF is unavailable") from None
    if (
        hashlib.sha256(content).hexdigest() != artifact.checksum_sha256.lower()
        or len(content) != artifact.file_size_bytes
    ):
        raise CatalogBuildArtifactIntegrityError("Catalog PDF failed integrity validation")
    try:
        page_count = validate_catalog_pdf(content)
    except Exception:
        raise CatalogBuildArtifactIntegrityError("Catalog PDF failed integrity validation") from None
    if page_count != artifact.page_count:
        raise CatalogBuildArtifactIntegrityError("Catalog PDF metadata is invalid")
    return path, artifact


def hash_catalog_build_request(request: CatalogBuildCreate) -> str:
    identity = {
        "product_ids": sorted(str(value) for value in request.product_ids),
        "catalog_brand_profile_id": str(request.catalog_brand_profile_id),
        "layout_key": request.layout_key,
        "layout_version": request.layout_version,
        "currency": request.currency,
    }
    if request.theme_key is not None:
        identity.update({
            "theme_key": request.theme_key,
            "theme_version": request.theme_version,
            "primary_color_override": request.primary_color_override,
            "accent_color_override": request.accent_color_override,
        })
    if request.cover is not None:
        identity["cover"] = request.cover.model_dump(mode="json", exclude_none=True)
    if request.closing is not None:
        identity["closing"] = request.closing.model_dump(mode="json", exclude_none=True)
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_by_idempotency_key(
    session: Session, idempotency_key: str
) -> CatalogBuild | None:
    return session.scalar(
        select(CatalogBuild).where(CatalogBuild.idempotency_key == idempotency_key)
    )


def _require_matching_request(build: CatalogBuild, request_hash: str) -> None:
    if build.request_hash.lower() != request_hash:
        raise CatalogBuildIdempotencyConflictError(
            "idempotency key was already used for a different Catalog build request"
        )


def _normalized_status(status: JobStatus) -> str:
    return {
        JobStatus.QUEUED: "queued",
        JobStatus.RUNNING: "running",
        JobStatus.SUCCEEDED: "succeeded",
        JobStatus.FAILED: "failed",
    }[status]


def _latest_render_run(
    session: Session, job_id: uuid.UUID
) -> CatalogRenderRun | None:
    return session.scalar(
        select(CatalogRenderRun)
        .where(CatalogRenderRun.job_id == job_id)
        .order_by(CatalogRenderRun.created_at.desc(), CatalogRenderRun.id.desc())
        .limit(1)
    )


def _successful_artifact_for_job(
    session: Session, job_id: uuid.UUID
) -> CatalogArtifact | None:
    return session.scalar(
        select(CatalogArtifact)
        .join(CatalogRenderRun, CatalogRenderRun.id == CatalogArtifact.render_run_id)
        .where(
            CatalogRenderRun.job_id == job_id,
            CatalogRenderRun.status == ExtractionRunStatus.SUCCEEDED,
        )
        .order_by(CatalogArtifact.created_at.desc(), CatalogArtifact.id.desc())
        .limit(1)
    )


def _public_render_error(value: str | None) -> str:
    if not value:
        return "Catalog rendering failed."
    message = " ".join(value.lower().split())
    if "chromium" in message or "browser" in message:
        return "Chromium could not render the catalog PDF."
    if "snapshot" in message or "asset" in message or "logo" in message or "hero" in message:
        return "A frozen catalog asset is unavailable or invalid."
    if "template" in message or "configuration" in message:
        return "The catalog render configuration is no longer supported."
    if "pdf" in message or "storage" in message:
        return "The catalog PDF could not be saved or validated."
    return "Catalog rendering failed."
