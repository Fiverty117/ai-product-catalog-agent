import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, Product, ProductCopyManualRevision, ProductCopyReview, ProductCopyRun
from app.domain.enums import JobStatus, ProductCopyResolutionState
from app.domain.schemas import (
    ProductCopyEditorialEffectiveSummary,
    ProductCopyEditorialReviewRequest,
    ProductCopyEditorialReviewSummary,
    ProductCopyEditorialRunSummary,
    ProductCopyEditorialSummary,
    ProductCopyGenerationSummary,
    ProductCopyManualRevisionRequest,
    ProductCopyManualRevisionSummary,
    ProductCopyReviewRequest,
)
from app.services.catalog_builder import get_catalog_builder_product_summary
from app.services.jobs import JobRecoveryError, requeue_failed_job
from app.services.product_copy import (
    PRODUCT_COPY_JOB_TYPE,
    enqueue_product_copy,
    is_product_copy_run_stale,
    build_product_copy_input_snapshot,
    build_product_copy_source_fingerprint,
)
from app.domain.enums import ProductCopyResolutionState, ProductCopyType
from app.services.product_copy_review import apply_product_copy_review, resolve_effective_product_copy


class ProductCopyEditorialError(ValueError):
    pass


class UnknownEditorialProductError(ProductCopyEditorialError):
    pass


class UnknownEditorialRunError(ProductCopyEditorialError):
    pass


class UnknownEditorialGenerationError(ProductCopyEditorialError):
    pass


class EditorialGenerationRetryError(ProductCopyEditorialError):
    pass


class NoCurrentProductCopyError(ProductCopyEditorialError):
    pass


def create_manual_product_copy_revision(
    session: Session, *, product_id: uuid.UUID, request: ProductCopyManualRevisionRequest,
) -> ProductCopyManualRevision:
    if session.get(Product, product_id) is None:
        raise UnknownEditorialProductError(f"Product not found: {product_id}")
    if resolve_effective_product_copy(session, product_id).state is not ProductCopyResolutionState.CURRENT:
        raise NoCurrentProductCopyError("Only current Product copy can be edited.")
    snapshot = build_product_copy_input_snapshot(session, product_id)
    revision = ProductCopyManualRevision(
        product_id=product_id, copy_type=ProductCopyType.SHORT_DESCRIPTION,
        short_description=request.short_description,
        source_fingerprint=build_product_copy_source_fingerprint(snapshot),
        input_snapshot=snapshot.model_dump(mode="json", exclude_none=True),
    )
    session.add(revision)
    session.flush()
    return revision


def manual_revision_summary(revision: ProductCopyManualRevision, current_fingerprint: str) -> ProductCopyManualRevisionSummary:
    return ProductCopyManualRevisionSummary(
        revision_id=revision.id, short_description=revision.short_description,
        source_state="current" if revision.source_fingerprint.lower() == current_fingerprint else "stale",
        created_at=revision.created_at,
    )


def get_product_copy_editorial_summary(
    session: Session,
    *,
    product_id: uuid.UUID,
) -> ProductCopyEditorialSummary:
    if session.get(Product, product_id) is None:
        raise UnknownEditorialProductError(f"Product not found: {product_id}")

    product = get_catalog_builder_product_summary(
        session,
        product_id=product_id,
    )
    effective = resolve_effective_product_copy(session, product_id)
    jobs = _product_generation_jobs(session, product_id)
    runs = session.scalars(
        select(ProductCopyRun)
        .where(ProductCopyRun.product_id == product_id)
        .order_by(ProductCopyRun.created_at.desc(), ProductCopyRun.id.desc())
    ).all()
    revisions = session.scalars(
        select(ProductCopyManualRevision)
        .where(ProductCopyManualRevision.product_id == product_id)
        .order_by(ProductCopyManualRevision.created_at.desc(), ProductCopyManualRevision.id.desc())
    ).all()
    fingerprint = build_product_copy_source_fingerprint(build_product_copy_input_snapshot(session, product_id))
    return ProductCopyEditorialSummary(
        product=product,
        effective_copy=ProductCopyEditorialEffectiveSummary(
            state=effective.state,
            short_description=(
                effective.short_description
                if effective.state is ProductCopyResolutionState.CURRENT
                else None
            ),
        ),
        generations=[_generation_summary(job) for job in jobs],
        runs=[_run_summary(session, run) for run in runs],
        manual_revisions=[manual_revision_summary(revision, fingerprint) for revision in revisions],
        has_active_generation=any(
            job.status in (JobStatus.QUEUED, JobStatus.RUNNING) for job in jobs
        ),
    )


def enqueue_editorial_product_copy(
    session: Session,
    *,
    product_id: uuid.UUID,
    generation_request_key: str | None = None,
) -> Job:
    if session.get(Product, product_id) is None:
        raise UnknownEditorialProductError(f"Product not found: {product_id}")
    return enqueue_product_copy(
        session,
        product_id=product_id,
        generation_request_key=generation_request_key,
    )


def review_editorial_product_copy(
    session: Session,
    *,
    product_id: uuid.UUID,
    run_id: uuid.UUID,
    request: ProductCopyEditorialReviewRequest,
) -> ProductCopyReview:
    if session.get(Product, product_id) is None:
        raise UnknownEditorialProductError(f"Product not found: {product_id}")
    run = session.get(ProductCopyRun, run_id)
    if run is None or run.product_id != product_id:
        raise UnknownEditorialRunError(f"Product copy run not found: {run_id}")
    return apply_product_copy_review(
        session,
        ProductCopyReviewRequest(
            product_copy_run_id=run.id,
            decision=request.decision,
            corrected_short_description=request.corrected_short_description,
        ),
    )


def retry_editorial_generation(
    session: Session,
    *,
    product_id: uuid.UUID,
    job_id: uuid.UUID,
) -> Job:
    if session.get(Product, product_id) is None:
        raise UnknownEditorialProductError(f"Product not found: {product_id}")
    job = session.get(Job, job_id)
    if job is None or not _job_belongs_to_product(job, product_id):
        raise UnknownEditorialGenerationError(f"Product copy Job not found: {job_id}")
    try:
        return requeue_failed_job(session, job)
    except JobRecoveryError as exc:
        raise EditorialGenerationRetryError(str(exc)) from exc


def generation_summary(job: Job) -> ProductCopyGenerationSummary:
    return _generation_summary(job)


def review_summary(review: ProductCopyReview) -> ProductCopyEditorialReviewSummary:
    return _review_summary(review)


def _product_generation_jobs(session: Session, product_id: uuid.UUID) -> list[Job]:
    jobs = session.scalars(
        select(Job)
        .where(Job.job_type == PRODUCT_COPY_JOB_TYPE)
        .order_by(Job.created_at.desc(), Job.id.desc())
    ).all()
    return [job for job in jobs if _job_belongs_to_product(job, product_id)]


def _job_belongs_to_product(job: Job, product_id: uuid.UUID) -> bool:
    return str(job.payload.get("product_id", "")) == str(product_id)


def _generation_summary(job: Job) -> ProductCopyGenerationSummary:
    return ProductCopyGenerationSummary(
        job_id=job.id,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        can_retry=(job.status is JobStatus.FAILED and job.attempts < job.max_attempts),
        error=job.last_error if job.status is JobStatus.FAILED else None,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
    )


def _run_summary(
    session: Session,
    run: ProductCopyRun,
) -> ProductCopyEditorialRunSummary:
    review = run.review
    return ProductCopyEditorialRunSummary(
        run_id=run.id,
        job_id=run.job_id,
        status=run.status,
        review_state=(review.decision.value if review is not None else "unreviewed"),
        generated_text=run.generated_text,
        sanitized_error=run.sanitized_error,
        source_state=("stale" if is_product_copy_run_stale(session, run) else "current"),
        review=_review_summary(review) if review is not None else None,
        provider=run.provider,
        model=run.model,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def _review_summary(review: ProductCopyReview) -> ProductCopyEditorialReviewSummary:
    return ProductCopyEditorialReviewSummary(
        decision=review.decision,
        corrected_short_description=review.corrected_short_description,
        created_at=review.created_at,
    )
