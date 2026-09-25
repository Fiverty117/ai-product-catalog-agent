"""Read-only Catalog History endpoints."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.catalog_history import CatalogHistoryDetail, CatalogHistoryOptions, CatalogHistoryPage
from app.domain.catalog_duplication import CatalogDuplicateTemplate
from app.services.catalog_duplication import UnknownDuplicateSourceError, get_catalog_duplicate_template
from app.services.catalog_history import (
    UnknownHistoryBuildError, catalog_history_options,
    get_catalog_history_detail, list_catalog_history,
)

router = APIRouter(prefix="/api/catalogs", tags=["catalog-history"])


@router.get("", response_model=CatalogHistoryPage)
def list_catalogs(
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str, Query(max_length=120)] = "",
    status: Literal["all", "ready", "active", "failed"] = "all",
    publisher: Annotated[str | None, Query(max_length=255)] = None,
    theme: Annotated[str | None, Query(max_length=100)] = None,
    period: Literal["all", "today", "7d", "30d"] = "all",
) -> CatalogHistoryPage:
    return list_catalog_history(
        session, page=page, page_size=page_size, search=search,
        status=status, publisher=publisher, theme=theme, period=period,
    )


@router.get("/options", response_model=CatalogHistoryOptions)
def history_options(session: Annotated[Session, Depends(get_db)]) -> CatalogHistoryOptions:
    return catalog_history_options(session)


@router.get("/{build_id}", response_model=CatalogHistoryDetail)
def catalog_detail(
    build_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> CatalogHistoryDetail:
    try:
        return get_catalog_history_detail(session, build_id)
    except UnknownHistoryBuildError as exc:
        raise HTTPException(404, "Catalog not found.") from exc


@router.get("/{build_id}/duplicate-template", response_model=CatalogDuplicateTemplate)
def duplicate_template(
    build_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> CatalogDuplicateTemplate:
    try:
        return get_catalog_duplicate_template(session, build_id)
    except UnknownDuplicateSourceError as exc:
        raise HTTPException(404, "Catalog not found.") from exc
