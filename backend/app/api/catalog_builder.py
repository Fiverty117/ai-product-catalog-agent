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
    CatalogBuilderProductList,
)
from app.services.catalog_branding import (
    DEFAULT_STORAGE_ROOT,
    CatalogBrandingError,
    get_active_catalog_brand_profile,
    resolve_catalog_branding,
)
from app.services.catalog_builder import (
    CatalogBuilderImageUnavailableError,
    UnknownCatalogBuilderProductError,
    list_catalog_builder_brand_profiles,
    list_catalog_builder_layouts,
    list_catalog_builder_products,
    resolve_catalog_builder_product_image,
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
