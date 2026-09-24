import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import (
    CatalogBuilderBrandProfileSummary,
    CatalogBuilderLayoutSummary,
    CatalogBuilderThemeSummary,
    CatalogBuilderProductList,
    CatalogBuildCreate,
    CatalogBuildRead,
)
from app.services.catalog_branding import (
    DEFAULT_STORAGE_ROOT,
    CatalogBrandingError,
    InactiveCatalogBrandProfileError,
    UnknownCatalogBrandProfileError,
    get_active_catalog_brand_profile,
    resolve_catalog_branding,
)
from app.services.catalog_builds import (
    CatalogBuildArtifactIntegrityError,
    CatalogBuildIdempotencyConflictError,
    CatalogBuildIntegrityError,
    CatalogBuildRetryError,
    UnknownCatalogBuildArtifactError,
    UnknownCatalogBuildError,
    create_catalog_build,
    get_catalog_build,
    resolve_catalog_build_artifact_pdf,
    retry_catalog_build,
)
from app.services.catalog_builder import (
    CatalogBuilderImageUnavailableError,
    UnknownCatalogBuilderProductError,
    list_catalog_builder_brand_profiles,
    list_catalog_builder_layouts,
    list_catalog_builder_products,
    resolve_catalog_builder_product_image,
)
from app.rendering.catalog_layouts import UnknownCatalogLayoutError
from app.rendering.catalog_themes import InvalidCatalogPaletteError, UnknownCatalogThemeError, catalog_theme_definitions
from app.services.catalog_readiness import UnknownCatalogReadinessProductError
from app.services.catalog_snapshots import (
    CatalogSnapshotError,
    CatalogSnapshotIntegrityError,
    CatalogSnapshotReadinessError,
)

router = APIRouter(prefix="/api/catalog-builder", tags=["catalog-builder"])


@router.get("/products", response_model=CatalogBuilderProductList)
def list_products(
    session: Annotated[Session, Depends(get_db)],
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "PYG",
    search: Annotated[str | None, Query(max_length=200)] = None,
    readiness: Literal["all", "ready", "not_ready"] = "all",
) -> CatalogBuilderProductList:
    return list_catalog_builder_products(
        session,
        currency=currency,
        search=search,
        readiness=readiness,
    )


@router.get(
    "/brand-profiles",
    response_model=list[CatalogBuilderBrandProfileSummary],
)
def list_brand_profiles(
    session: Annotated[Session, Depends(get_db)],
) -> list[CatalogBuilderBrandProfileSummary]:
    return list_catalog_builder_brand_profiles(session)


@router.get("/layouts", response_model=list[CatalogBuilderLayoutSummary])
def list_layouts() -> list[CatalogBuilderLayoutSummary]:
    return list_catalog_builder_layouts()


@router.get("/themes", response_model=list[CatalogBuilderThemeSummary])
def list_themes() -> list[CatalogBuilderThemeSummary]:
    return [CatalogBuilderThemeSummary(
        key=theme.key, version=theme.version,
        display_name=theme.display_name, description=theme.description,
    ) for theme in catalog_theme_definitions()]


@router.post("/builds", response_model=CatalogBuildRead)
def create_build(
    request: CatalogBuildCreate,
    session: Annotated[Session, Depends(get_db)],
) -> CatalogBuildRead:
    try:
        build = create_catalog_build(session, request)
        session.commit()
        return build
    except UnknownCatalogReadinessProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        ) from exc
    except UnknownCatalogBrandProfileError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog Brand profile not found.",
        ) from exc
    except UnknownCatalogLayoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog layout not found.",
        ) from exc
    except UnknownCatalogThemeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidCatalogPaletteError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except CatalogSnapshotReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "catalog_readiness_changed",
                "message": "One or more selected Products are no longer ready.",
                "products": [
                    {
                        "product_id": str(report.product_id),
                        "blockers": [
                            blocker.model_dump(mode="json")
                            for blocker in report.blockers
                        ],
                    }
                    for report in exc.failures
                ],
            },
        ) from exc
    except (
        InactiveCatalogBrandProfileError,
        CatalogBuildIdempotencyConflictError,
        CatalogSnapshotError,
        CatalogBrandingError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get("/builds/{build_id}", response_model=CatalogBuildRead)
def read_build(
    build_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> CatalogBuildRead:
    try:
        return get_catalog_build(session, build_id)
    except UnknownCatalogBuildError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog build not found.",
        ) from exc
    except (CatalogBuildIntegrityError, CatalogSnapshotIntegrityError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Catalog build state is inconsistent.",
        ) from exc


@router.post("/builds/{build_id}/retry", response_model=CatalogBuildRead)
def retry_build(
    build_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> CatalogBuildRead:
    try:
        build = retry_catalog_build(session, build_id)
        session.commit()
        return build
    except UnknownCatalogBuildError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog build not found.",
        ) from exc
    except CatalogBuildRetryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (CatalogBuildIntegrityError, CatalogSnapshotIntegrityError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Catalog build state is inconsistent.",
        ) from exc


@router.get("/artifacts/{artifact_id}/pdf", response_class=FileResponse)
def catalog_artifact_pdf(
    artifact_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
    download: bool = False,
) -> FileResponse:
    try:
        path, artifact = resolve_catalog_build_artifact_pdf(
            session, artifact_id
        )
    except UnknownCatalogBuildArtifactError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog PDF not found.",
        ) from exc
    except CatalogBuildArtifactIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Catalog PDF is unavailable or failed integrity validation.",
        ) from exc
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"catalog-{artifact.id}.pdf",
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/products/{product_id}/image", response_class=FileResponse)
def product_image(
    product_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> FileResponse:
    try:
        path, media_type = resolve_catalog_builder_product_image(
            session,
            product_id=product_id,
        )
    except UnknownCatalogBuilderProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        ) from exc
    except CatalogBuilderImageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product image is unavailable.",
        ) from exc
    return FileResponse(path, media_type=media_type)


@router.get("/brand-profiles/{profile_id}/logo", response_class=FileResponse)
def brand_profile_logo(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> FileResponse:
    try:
        profile = get_active_catalog_brand_profile(session, profile_id=profile_id)
        branding = resolve_catalog_branding(profile)
    except CatalogBrandingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog brand logo is unavailable.",
        ) from exc
    if branding.logo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Catalog brand logo is unavailable.",
        )
    path = Path(DEFAULT_STORAGE_ROOT) / branding.logo.storage_relative_path
    return FileResponse(path, media_type=branding.logo.mime_type)
