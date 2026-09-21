import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import (
    ProductCopyEditorialReviewRequest,
    ProductCopyEditorialSummary,
    ProductCopyGenerationResponse,
    ProductCopyReviewResponse,
    ProductCopyManualRevisionRequest,
    ProductCopyManualRevisionResponse,
)
from app.services.product_copy_review import (
    DuplicateProductCopyReviewError,
    InvalidProductCopyResultError,
    UnreviewableProductCopyRunError,
)
from app.services.product_copy_editorial import (
    EditorialGenerationRetryError,
    UnknownEditorialGenerationError,
    UnknownEditorialProductError,
    UnknownEditorialRunError,
    NoCurrentProductCopyError,
    create_manual_product_copy_revision,
    enqueue_editorial_product_copy,
    generation_summary,
    get_product_copy_editorial_summary,
    retry_editorial_generation,
    review_editorial_product_copy,
    review_summary,
    manual_revision_summary,
)
from app.services.product_copy import build_product_copy_input_snapshot, build_product_copy_source_fingerprint

router = APIRouter(prefix="/api/products", tags=["product-copy-editorial"])


@router.post(
    "/{product_id}/product-copy/manual-revisions",
    response_model=ProductCopyManualRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def save_manual_product_copy_revision(
    product_id: uuid.UUID,
    request: ProductCopyManualRevisionRequest,
    session: Annotated[Session, Depends(get_db)],
) -> ProductCopyManualRevisionResponse:
    try:
        revision = create_manual_product_copy_revision(session, product_id=product_id, request=request)
        session.commit()
        fingerprint = build_product_copy_source_fingerprint(build_product_copy_input_snapshot(session, product_id))
        return ProductCopyManualRevisionResponse(
            revision=manual_revision_summary(revision, fingerprint),
            editorial=get_product_copy_editorial_summary(session, product_id=product_id),
        )
    except UnknownEditorialProductError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Product not found.") from exc
    except NoCurrentProductCopyError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/{product_id}/product-copy",
    response_model=ProductCopyEditorialSummary,
)
def editorial_summary(
    product_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> ProductCopyEditorialSummary:
    try:
        return get_product_copy_editorial_summary(session, product_id=product_id)
    except UnknownEditorialProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        ) from exc


@router.post(
    "/{product_id}/product-copy/generations",
    response_model=ProductCopyGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_product_copy(
    product_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ] = None,
) -> ProductCopyGenerationResponse:
    try:
        job = enqueue_editorial_product_copy(
            session,
            product_id=product_id,
            generation_request_key=idempotency_key,
        )
        session.commit()
        return ProductCopyGenerationResponse(
            generation=generation_summary(job),
            editorial=get_product_copy_editorial_summary(
                session,
                product_id=product_id,
            ),
        )
    except UnknownEditorialProductError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        ) from exc
    except (ValueError, ValidationError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Product copy generation request is invalid.",
        ) from exc


@router.post(
    "/{product_id}/product-copy/generations/{job_id}/retry",
    response_model=ProductCopyGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_product_copy_generation(
    product_id: uuid.UUID,
    job_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db)],
) -> ProductCopyGenerationResponse:
    try:
        job = retry_editorial_generation(
            session,
            product_id=product_id,
            job_id=job_id,
        )
        session.commit()
        return ProductCopyGenerationResponse(
            generation=generation_summary(job),
            editorial=get_product_copy_editorial_summary(
                session,
                product_id=product_id,
            ),
        )
    except (UnknownEditorialProductError, UnknownEditorialGenerationError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product copy generation was not found.",
        ) from exc
    except EditorialGenerationRetryError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/{product_id}/product-copy/runs/{run_id}/review",
    response_model=ProductCopyReviewResponse,
)
def review_product_copy(
    product_id: uuid.UUID,
    run_id: uuid.UUID,
    request: ProductCopyEditorialReviewRequest,
    session: Annotated[Session, Depends(get_db)],
) -> ProductCopyReviewResponse:
    try:
        review = review_editorial_product_copy(
            session,
            product_id=product_id,
            run_id=run_id,
            request=request,
        )
        session.commit()
        return ProductCopyReviewResponse(
            review=review_summary(review),
            editorial=get_product_copy_editorial_summary(
                session,
                product_id=product_id,
            ),
        )
    except (UnknownEditorialProductError, UnknownEditorialRunError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product copy proposal was not found.",
        ) from exc
    except (
        DuplicateProductCopyReviewError,
        UnreviewableProductCopyRunError,
        InvalidProductCopyResultError,
        IntegrityError,
    ) as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product copy proposal can no longer be reviewed.",
        ) from exc
