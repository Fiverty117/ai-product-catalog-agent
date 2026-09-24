import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.models import CatalogRenderRun
from app.domain.schemas import CatalogRenderJobPayload, CatalogRenderJobPayloadV2, CatalogRenderJobPayloadV3, CatalogRenderJobPayloadV4, CatalogSnapshotData
from app.rendering.catalog_pdf import (
    CatalogPdfRenderer,
    RetryableCatalogRendererError,
)
from app.services.catalog_rendering import (
    CATALOG_RENDERER_VERSION,
    CATALOG_RENDER_JOB_TYPE,
    CATALOG_RENDER_JOB_TYPE_V2,
    CATALOG_RENDER_JOB_TYPE_V3,
    CATALOG_RENDER_JOB_TYPE_V4,
    DEFAULT_CATALOGS_DIR,
    DEFAULT_STORAGE_ROOT,
    DEFAULT_TEMPLATE_ROOT,
    CatalogPdfStorageError,
    CatalogRenderError,
    InvalidCatalogRenderJobError,
    build_catalog_render_view_model,
    complete_catalog_render_run,
    create_running_catalog_render_run,
    hash_catalog_template,
    mark_catalog_render_run_failed,
    normalize_catalog_render_config,
    render_catalog_html,
    resolve_catalog_template,
    store_catalog_pdf,
)
from app.services.catalog_branding import CatalogBrandingError, hash_resolved_catalog_branding
from app.rendering.catalog_themes import UnknownCatalogThemeError, hash_resolved_catalog_theme, resolve_catalog_theme_definition
from app.rendering.catalog_covers import UnknownCatalogCoverError, hash_resolved_catalog_cover, resolve_catalog_cover_definition
from app.services.catalog_cover_assets import CatalogCoverAssetError
from app.services.jobs import PermanentJobError
from app.workers.job_worker import ClaimedJob

SessionFactory = Callable[[], Session]


class PermanentCatalogRenderError(PermanentJobError):
    pass


class CatalogRenderJobHandler:
    def __init__(
        self,
        session_factory: SessionFactory,
        renderer: CatalogPdfRenderer,
        *,
        storage_root: Path = DEFAULT_STORAGE_ROOT,
        catalogs_dir: Path = DEFAULT_CATALOGS_DIR,
        template_root: Path = DEFAULT_TEMPLATE_ROOT,
        renderer_version: str = CATALOG_RENDERER_VERSION,
    ) -> None:
        self._session_factory = session_factory
        self._renderer = renderer
        self._storage_root = storage_root
        self._catalogs_dir = catalogs_dir
        self._template_root = template_root
        self._renderer_version = renderer_version

    def __call__(self, claimed: ClaimedJob) -> None:
        payload, template = self._validate_payload(claimed)
        run_id, snapshot = self._prepare_attempt(claimed, payload)
        try:
            view_model = build_catalog_render_view_model(
                snapshot,
                payload.config,
                storage_root=self._storage_root,
                store_name=template.display_name,
                branding=payload.branding_data if isinstance(payload, CatalogRenderJobPayloadV2) else None,
                theme=payload.theme_data if isinstance(payload, CatalogRenderJobPayloadV3) else None,
                cover=payload.cover_data if isinstance(payload, CatalogRenderJobPayloadV4) else None,
            )
            if hash_catalog_template(template) != payload.template_hash.lower():
                raise InvalidCatalogRenderJobError(
                    "catalog template changed after this Job was enqueued"
                )
            html = render_catalog_html(view_model, template)
            render_result = self._renderer.render(html, payload.config)
            stored_pdf = store_catalog_pdf(
                render_result.pdf_bytes,
                catalogs_dir=self._catalogs_dir,
            )
        except Exception as error:
            safe_error = _safe_render_error(error)
            self._fail_attempt(run_id, safe_error)
            raise safe_error from None

        try:
            with self._session_factory() as session:
                run = _require_run(session, run_id)
                complete_catalog_render_run(
                    session,
                    run,
                    stored_pdf=stored_pdf,
                    renderer_engine=render_result.engine,
                    renderer_engine_version=render_result.engine_version,
                )
                session.commit()
        except Exception:
            error = RetryableCatalogRendererError(
                "catalog render database completion failed"
            )
            self._fail_attempt(run_id, error)
            raise error from None

    def _validate_payload(self, claimed: ClaimedJob):
        if claimed.job_type not in (CATALOG_RENDER_JOB_TYPE, CATALOG_RENDER_JOB_TYPE_V2, CATALOG_RENDER_JOB_TYPE_V3, CATALOG_RENDER_JOB_TYPE_V4):
            raise PermanentCatalogRenderError(
                "handler requires a supported catalog render job type"
            )
        try:
            payload_type = CatalogRenderJobPayloadV4 if claimed.job_type == CATALOG_RENDER_JOB_TYPE_V4 else CatalogRenderJobPayloadV3 if claimed.job_type == CATALOG_RENDER_JOB_TYPE_V3 else CatalogRenderJobPayloadV2 if claimed.job_type == CATALOG_RENDER_JOB_TYPE_V2 else CatalogRenderJobPayload
            payload = payload_type.model_validate(claimed.payload)
            normalized_config = normalize_catalog_render_config(payload.config)
            template = resolve_catalog_template(
                payload.template_key,
                template_root=self._template_root,
            )
        except (ValidationError, CatalogRenderError):
            raise PermanentCatalogRenderError(
                "invalid catalog render Job payload or template"
            ) from None
        if (
            normalized_config != payload.config
            or payload.renderer_version != self._renderer_version
            or payload.template_version != template.version
            or payload.template_hash.lower() != hash_catalog_template(template)
        ):
            raise PermanentCatalogRenderError(
                "catalog render Job configuration is no longer supported"
            )
        if isinstance(payload, CatalogRenderJobPayloadV2) and hash_resolved_catalog_branding(payload.branding_data) != payload.branding_hash.lower():
            raise PermanentCatalogRenderError("frozen branding payload hash mismatch")
        if isinstance(payload, CatalogRenderJobPayloadV3):
            try:
                definition = resolve_catalog_theme_definition(payload.theme_data.theme_key, payload.theme_data.theme_version)
            except UnknownCatalogThemeError:
                raise PermanentCatalogRenderError("frozen catalog theme is no longer supported") from None
            if payload.theme_data.css_class != definition.css_class or hash_resolved_catalog_theme(payload.theme_data) != payload.theme_hash.lower():
                raise PermanentCatalogRenderError("frozen theme payload hash mismatch")
        if isinstance(payload, CatalogRenderJobPayloadV4):
            try:
                if payload.cover_data.enabled:
                    resolve_catalog_cover_definition(payload.cover_data.cover_key or "", payload.cover_data.cover_version or "")
            except UnknownCatalogCoverError:
                raise PermanentCatalogRenderError("frozen catalog cover is no longer supported") from None
            if hash_resolved_catalog_cover(payload.cover_data) != payload.cover_hash.lower():
                raise PermanentCatalogRenderError("frozen cover payload hash mismatch")
        return payload, template

    def _prepare_attempt(
        self,
        claimed: ClaimedJob,
        payload: CatalogRenderJobPayload | CatalogRenderJobPayloadV2 | CatalogRenderJobPayloadV3 | CatalogRenderJobPayloadV4,
    ) -> tuple[uuid.UUID, CatalogSnapshotData]:
        with self._session_factory() as session:
            try:
                run, snapshot = create_running_catalog_render_run(
                    session,
                    payload=payload,
                    job_id=claimed.id,
                )
            except CatalogRenderError as error:
                raise PermanentCatalogRenderError(str(error)) from None
            session.commit()
            return run.id, snapshot

    def _fail_attempt(self, run_id: uuid.UUID, error: Exception) -> None:
        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_catalog_render_run_failed(session, run, error=error)
            session.commit()


def catalog_render_handlers(
    session_factory: SessionFactory,
    renderer: CatalogPdfRenderer,
    **handler_options,
) -> dict[str, CatalogRenderJobHandler]:
    handler = CatalogRenderJobHandler(session_factory, renderer, **handler_options)
    return {CATALOG_RENDER_JOB_TYPE: handler, CATALOG_RENDER_JOB_TYPE_V2: handler, CATALOG_RENDER_JOB_TYPE_V3: handler, CATALOG_RENDER_JOB_TYPE_V4: handler}


def _safe_render_error(error: Exception) -> Exception:
    if isinstance(error, (PermanentJobError, RetryableCatalogRendererError)):
        return error
    if isinstance(error, CatalogRenderError):
        return PermanentCatalogRenderError(str(error))
    if isinstance(error, CatalogBrandingError):
        return PermanentCatalogRenderError(str(error))
    if isinstance(error, CatalogCoverAssetError):
        return PermanentCatalogRenderError(str(error))
    if isinstance(error, CatalogPdfStorageError):
        return RetryableCatalogRendererError("catalog PDF storage failed")
    return RetryableCatalogRendererError("catalog render attempt failed")


def _require_run(session: Session, run_id: uuid.UUID) -> CatalogRenderRun:
    run = session.get(CatalogRenderRun, run_id)
    if run is None:
        raise RuntimeError("CatalogRenderRun disappeared during execution")
    return run
