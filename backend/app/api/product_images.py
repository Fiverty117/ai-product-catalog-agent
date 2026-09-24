import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import ProductDerivedImageReviewRequest, ProductImageEditorialSummary, ProductImageGenerationResponse, ProductImageGenerationSummary, ProductImagePresentationRequest
from app.services.image_presentation import (
    DerivedImageAssetIntegrityError, InvalidDerivedImageLineageError,
    UnapprovedDerivedImageError,
)
from app.services.product_image_editorial import (
    ProductImageAssetUnavailableError, ProductImageLifecycleConflictError,
    UnknownProductImageResourceError, get_product_image_editorial_summary,
    resolve_product_derived_asset, resolve_product_photo_asset,
    review_product_derived_image, select_product_image_presentation,
    enqueue_product_image_generation, get_product_image_generation,
    image_generation_summary, retry_product_image_generation,
)

router = APIRouter(prefix="/api/products", tags=["product-image-editorial"])
DB = Annotated[Session, Depends(get_db)]


@router.get("/{product_id}/images/editorial", response_model=ProductImageEditorialSummary)
def image_editorial(product_id: uuid.UUID, session: DB) -> ProductImageEditorialSummary:
    try:
        return get_product_image_editorial_summary(session, product_id)
    except UnknownProductImageResourceError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{product_id}/images/photos/{photo_id}/enhancements", response_model=ProductImageGenerationResponse, status_code=status.HTTP_202_ACCEPTED)
def generate_image(product_id: uuid.UUID, photo_id: uuid.UUID, session: DB, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]) -> ProductImageGenerationResponse:
    try:
        job = enqueue_product_image_generation(session, product_id, photo_id, idempotency_key)
        session.commit()
        return ProductImageGenerationResponse(generation=image_generation_summary(job), editorial=get_product_image_editorial_summary(session, product_id))
    except UnknownProductImageResourceError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ProductImageLifecycleConflictError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, "Invalid enhancement request.") from exc
    except IntegrityError:
        session.rollback()
        # A concurrent retry may have inserted the same unique Job key.
        try:
            job = enqueue_product_image_generation(session, product_id, photo_id, idempotency_key)
            session.commit()
            return ProductImageGenerationResponse(generation=image_generation_summary(job), editorial=get_product_image_editorial_summary(session, product_id))
        except (IntegrityError, ProductImageLifecycleConflictError) as exc:
            session.rollback()
            raise HTTPException(409, "Image enhancement request conflicts with current state.") from exc


@router.get("/{product_id}/images/enhancements/{job_id}", response_model=ProductImageGenerationSummary)
def image_generation_status(product_id: uuid.UUID, job_id: uuid.UUID, session: DB) -> ProductImageGenerationSummary:
    try:
        return image_generation_summary(get_product_image_generation(session, product_id, job_id))
    except UnknownProductImageResourceError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{product_id}/images/enhancements/{job_id}/retry", response_model=ProductImageGenerationResponse, status_code=status.HTTP_202_ACCEPTED)
def retry_image_generation(product_id: uuid.UUID, job_id: uuid.UUID, session: DB) -> ProductImageGenerationResponse:
    try:
        job = retry_product_image_generation(session, product_id, job_id)
        session.commit()
        return ProductImageGenerationResponse(generation=image_generation_summary(job), editorial=get_product_image_editorial_summary(session, product_id))
    except UnknownProductImageResourceError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ProductImageLifecycleConflictError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


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
