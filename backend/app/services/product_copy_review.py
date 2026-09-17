import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Product, ProductCopyReview, ProductCopyRun
from app.db.types import utc_now
from app.domain.enums import (
    ExtractionRunStatus,
    ProductCopyResolutionState,
    ProductCopyReviewDecision,
)
from app.domain.schemas import (
    EffectiveProductCopy,
    ProductCopyResult,
    ProductCopyReviewRequest,
)
from app.services.product_copy import (
    build_product_copy_input_snapshot,
    build_product_copy_source_fingerprint,
)


class ProductCopyReviewError(ValueError):
    pass


class UnknownProductCopyRunError(ProductCopyReviewError):
    pass


class UnreviewableProductCopyRunError(ProductCopyReviewError):
    pass


class DuplicateProductCopyReviewError(ProductCopyReviewError):
    pass


class InvalidProductCopyResultError(ProductCopyReviewError):
    pass


class UnknownProductCopyProductError(ProductCopyReviewError):
    pass


def apply_product_copy_review(
    session: Session,
    request: ProductCopyReviewRequest | Mapping[str, Any],
    *,
    applied_at: datetime | None = None,
) -> ProductCopyReview:
    validated = ProductCopyReviewRequest.model_validate(request)
    run = session.get(ProductCopyRun, validated.product_copy_run_id)
    if run is None:
        raise UnknownProductCopyRunError(
            f"ProductCopyRun not found: {validated.product_copy_run_id}"
        )
    if run.status is not ExtractionRunStatus.SUCCEEDED:
        raise UnreviewableProductCopyRunError(
            f"Product copy run must be succeeded: {run.id}"
        )
    if session.scalar(
        select(ProductCopyReview).where(ProductCopyReview.product_copy_run_id == run.id)
    ) is not None:
        raise DuplicateProductCopyReviewError(
            f"Product copy run already reviewed: {run.id}"
        )
    try:
        ProductCopyResult(short_description=run.generated_text)
    except ValidationError:
        raise InvalidProductCopyResultError(
            "successful Product copy run has invalid generated text"
        ) from None

    corrected = (
        validated.corrected_short_description
        if validated.decision is ProductCopyReviewDecision.CORRECTED
        else None
    )
    review = ProductCopyReview(
        product_copy_run=run,
        decision=validated.decision,
        corrected_short_description=corrected,
        applied_at=(
            None
            if validated.decision is ProductCopyReviewDecision.REJECTED
            else applied_at or utc_now()
        ),
    )
    session.add(review)
    session.flush()
    return review


def resolve_effective_product_copy(
    session: Session,
    product_id: uuid.UUID,
) -> EffectiveProductCopy:
    if session.get(Product, product_id) is None:
        raise UnknownProductCopyProductError(f"Product not found: {product_id}")
    current_snapshot = build_product_copy_input_snapshot(session, product_id)
    current_fingerprint = build_product_copy_source_fingerprint(current_snapshot)
    rows = session.execute(
        select(ProductCopyReview, ProductCopyRun)
        .join(
            ProductCopyRun,
            ProductCopyRun.id == ProductCopyReview.product_copy_run_id,
        )
        .where(
            ProductCopyRun.product_id == product_id,
            ProductCopyRun.status == ExtractionRunStatus.SUCCEEDED,
            ProductCopyReview.decision.in_(
                (
                    ProductCopyReviewDecision.APPROVED,
                    ProductCopyReviewDecision.CORRECTED,
                )
            ),
        )
        .order_by(ProductCopyReview.created_at.desc(), ProductCopyReview.id.desc())
    ).all()
    latest_stale: tuple[ProductCopyReview, ProductCopyRun] | None = None
    for review, run in rows:
        if latest_stale is None:
            latest_stale = (review, run)
        if run.source_fingerprint.lower() == current_fingerprint:
            return _resolved_copy(
                product_id,
                ProductCopyResolutionState.CURRENT,
                review,
                run,
            )
    if latest_stale is not None:
        review, run = latest_stale
        return _resolved_copy(
            product_id,
            ProductCopyResolutionState.STALE,
            review,
            run,
        )
    return EffectiveProductCopy(
        product_id=product_id,
        state=ProductCopyResolutionState.NONE,
        short_description=None,
        product_copy_run_id=None,
        product_copy_review_id=None,
        source_fingerprint=None,
    )


def _resolved_copy(
    product_id: uuid.UUID,
    state: ProductCopyResolutionState,
    review: ProductCopyReview,
    run: ProductCopyRun,
) -> EffectiveProductCopy:
    text = (
        review.corrected_short_description
        if review.decision is ProductCopyReviewDecision.CORRECTED
        else run.generated_text
    )
    return EffectiveProductCopy(
        product_id=product_id,
        state=state,
        short_description=text,
        product_copy_run_id=run.id,
        product_copy_review_id=review.id,
        source_fingerprint=run.source_fingerprint,
    )
