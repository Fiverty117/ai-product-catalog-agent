import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import ProductDerivedImageReviewRequest, ProductImageEditorialSummary, ProductImagePresentationRequest
from app.services.image_presentation import (
    DerivedImageAssetIntegrityError, InvalidDerivedImageLineageError,
    UnapprovedDerivedImageError,
)
from app.services.product_image_editorial import (
    ProductImageAssetUnavailableError, ProductImageLifecycleConflictError,
    UnknownProductImageResourceError, get_product_image_editorial_summary,
    resolve_product_derived_asset, resolve_product_photo_asset,
    review_product_derived_image, select_product_image_presentation,
)

router = APIRouter(prefix="/api/products", tags=["product-image-editorial"])
DB = Annotated[Session, Depends(get_db)]


@router.get("/{product_id}/images/editorial", response_model=ProductImageEditorialSummary)
def image_editorial(product_id: uuid.UUID, session: DB) -> ProductImageEditorialSummary:
    try:
        return get_product_image_editorial_summary(session, product_id)
    except UnknownProductImageResourceError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{product_id}/images/derived/{derived_image_id}/review", response_model=ProductImageEditorialSummary)
def review_image(product_id: uuid.UUID, derived_image_id: uuid.UUID, request: ProductDerivedImageReviewRequest, session: DB) -> ProductImageEditorialSummary:
    try:
        review_product_derived_image(session, product_id, derived_image_id, request)
        session.commit()
        return get_product_image_editorial_summary(session, product_id)
    except UnknownProductImageResourceError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except (ProductImageLifecycleConflictError, DerivedImageAssetIntegrityError, InvalidDerivedImageLineageError) as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.put("/{product_id}/images/photos/{photo_id}/presentation", response_model=ProductImageEditorialSummary)
def select_presentation(product_id: uuid.UUID, photo_id: uuid.UUID, request: ProductImagePresentationRequest, session: DB) -> ProductImageEditorialSummary:
    try:
        select_product_image_presentation(session, product_id, photo_id, request)
        session.commit()
        return get_product_image_editorial_summary(session, product_id)
    except UnknownProductImageResourceError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except (ProductImageLifecycleConflictError, UnapprovedDerivedImageError, DerivedImageAssetIntegrityError, InvalidDerivedImageLineageError) as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.get("/{product_id}/images/photos/{photo_id}", response_class=FileResponse)
def source_image(product_id: uuid.UUID, photo_id: uuid.UUID, session: DB) -> FileResponse:
    try:
        path, media_type = resolve_product_photo_asset(session, product_id, photo_id)
    except UnknownProductImageResourceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProductImageAssetUnavailableError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type=media_type)


@router.get("/{product_id}/images/derived/{derived_image_id}", response_class=FileResponse)
def derived_image(product_id: uuid.UUID, derived_image_id: uuid.UUID, session: DB) -> FileResponse:
    try:
        path, media_type = resolve_product_derived_asset(session, product_id, derived_image_id)
    except UnknownProductImageResourceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProductImageAssetUnavailableError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type=media_type)
