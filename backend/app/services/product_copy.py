import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.product_copy import (
    OPENAI_PRODUCT_COPY_PROVIDER,
    configured_openai_product_copy_model,
    normalize_product_copy_parameters,
)
from app.ai.prompts.product_copy_v1 import PROMPT_VERSION
from app.db.models import Category, Job, Product, ProductCategory, ProductCopyRun, SKU
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus, ProductCopyType
from app.domain.schemas import (
    ProductCopyCategorySnapshot,
    ProductCopyInputSnapshot,
    ProductCopyJobPayload,
    ProductCopyResult,
    ProductCopyVariantSnapshot,
)
from app.services.extraction import sanitize_extraction_error
from app.services.jobs import enqueue_job

PRODUCT_COPY_JOB_TYPE = "product.copy.v1"
PRODUCT_COPY_SCHEMA_VERSION = "product-copy-result-v1"


class ProductCopyRunError(ValueError):
    pass


class UnknownProductCopyProductError(ProductCopyRunError):
    pass


class InvalidProductCopyJobError(ProductCopyRunError):
    pass


class CompletedProductCopyRunError(ProductCopyRunError):
    pass


def build_product_copy_input_snapshot(
    session: Session,
    product_id: uuid.UUID,
) -> ProductCopyInputSnapshot:
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownProductCopyProductError(f"Product not found: {product_id}")
    category_rows = session.execute(
        select(ProductCategory, Category)
        .join(Category, Category.id == ProductCategory.category_id)
        .where(
            ProductCategory.product_id == product.id,
            Category.is_active.is_(True),
        )
        .order_by(
            ProductCategory.is_primary.desc(),
            Category.sort_order,
            Category.identity_key,
            Category.id,
        )
    ).all()
    primary = next(
        (
            ProductCopyCategorySnapshot(category_id=category.id, name=category.name)
            for assignment, category in category_rows
            if assignment.is_primary
        ),
        None,
    )
    secondary = [
        ProductCopyCategorySnapshot(category_id=category.id, name=category.name)
        for assignment, category in category_rows
        if not assignment.is_primary
    ]
    skus = session.scalars(
        select(SKU).where(SKU.product_id == product.id).order_by(SKU.id)
    ).all()
    return ProductCopyInputSnapshot(
        product_id=product.id,
        brand_name=product.brand.name,
        product_name=product.name,
        primary_category=primary,
        secondary_categories=secondary,
        variants=[
            ProductCopyVariantSnapshot(
                sku_id=sku.id,
                flavor=sku.flavor,
                size_value=sku.size_value,
                size_unit=sku.size_unit,
                servings=sku.servings,
            )
            for sku in skus
        ],
    )


def build_product_copy_source_fingerprint(
    input_snapshot: ProductCopyInputSnapshot,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            input_snapshot.model_dump(mode="json", exclude_none=True)
        ).encode("utf-8")
    ).hexdigest()


def build_product_copy_idempotency_key(
    payload: ProductCopyJobPayload,
    *,
    generation_request_key: str | None = None,
) -> str:
    expected_fingerprint = build_product_copy_source_fingerprint(
        payload.input_snapshot
    )
    if payload.source_fingerprint.lower() != expected_fingerprint:
        raise ValueError("Product copy source_fingerprint does not match its payload")
    identity = {
        "job_type": PRODUCT_COPY_JOB_TYPE,
        "product_id": str(payload.product_id),
        "copy_type": payload.copy_type,
        "source_fingerprint": expected_fingerprint,
        "provider": payload.provider,
        "model": payload.model,
        "prompt_version": payload.prompt_version,
        "schema_version": payload.schema_version,
        "parameters": payload.parameters,
    }
    if generation_request_key is not None:
        normalized_request_key = generation_request_key.strip()
        if not normalized_request_key or len(normalized_request_key) > 200:
            raise ValueError(
                "generation_request_key must contain 1 to 200 characters"
            )
        identity["generation_request_key_sha256"] = hashlib.sha256(
            normalized_request_key.encode("utf-8")
        ).hexdigest()
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"{PRODUCT_COPY_JOB_TYPE}:{digest}"


def enqueue_product_copy(
    session: Session,
    *,
    product_id: uuid.UUID,
    provider: str = OPENAI_PRODUCT_COPY_PROVIDER,
    model: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = PRODUCT_COPY_SCHEMA_VERSION,
    parameters: Mapping[str, object] | None = None,
    max_attempts: int = 3,
    generation_request_key: str | None = None,
) -> Job:
    resolved_parameters = normalize_product_copy_parameters(parameters)
    resolved_model = model or configured_openai_product_copy_model()
    snapshot = build_product_copy_input_snapshot(session, product_id)
    fingerprint = build_product_copy_source_fingerprint(snapshot)
    payload = ProductCopyJobPayload(
        product_id=product_id,
        copy_type=ProductCopyType.SHORT_DESCRIPTION.value,
        provider=provider,
        model=resolved_model,
        prompt_version=prompt_version,
        schema_version=schema_version,
        parameters=resolved_parameters,
        source_fingerprint=fingerprint,
        input_snapshot=snapshot,
    )
    return enqueue_job(
        session,
        job_type=PRODUCT_COPY_JOB_TYPE,
        payload=payload.model_dump(mode="json", exclude_none=True),
        idempotency_key=build_product_copy_idempotency_key(
            payload,
            generation_request_key=generation_request_key,
        ),
        max_attempts=max_attempts,
    )


def create_running_product_copy_run(
    session: Session,
    *,
    payload: ProductCopyJobPayload,
    job_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
) -> ProductCopyRun:
    if payload.product_id != payload.input_snapshot.product_id:
        raise InvalidProductCopyJobError(
            "job Product does not match its input snapshot"
        )
    build_product_copy_idempotency_key(payload)
    product = session.get(Product, payload.product_id)
    if product is None:
        raise UnknownProductCopyProductError(
            f"Product not found: {payload.product_id}"
        )
    job = None
    if job_id is not None:
        job = session.get(Job, job_id)
        if job is None or job.job_type != PRODUCT_COPY_JOB_TYPE:
            raise InvalidProductCopyJobError(
                f"job must exist with type {PRODUCT_COPY_JOB_TYPE}"
            )
    run = ProductCopyRun(
        product=product,
        job=job,
        copy_type=ProductCopyType.SHORT_DESCRIPTION,
        provider=payload.provider,
        model=payload.model,
        prompt_version=payload.prompt_version,
        schema_version=payload.schema_version,
        parameters=payload.parameters,
        source_fingerprint=payload.source_fingerprint.lower(),
        input_snapshot=payload.input_snapshot.model_dump(
            mode="json", exclude_none=True
        ),
        status=ExtractionRunStatus.RUNNING,
        started_at=started_at or utc_now(),
    )
    session.add(run)
    session.flush()
    return run


def mark_product_copy_run_succeeded(
    session: Session,
    run: ProductCopyRun,
    *,
    structured_result: ProductCopyResult | Mapping[str, Any],
    usage: Mapping[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> ProductCopyRun:
    _require_running(run)
    result = ProductCopyResult.model_validate(structured_result)
    run.status = ExtractionRunStatus.SUCCEEDED
    run.generated_text = result.short_description
    run.usage = dict(usage) if usage is not None else None
    run.sanitized_error = None
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def mark_product_copy_run_failed(
    session: Session,
    run: ProductCopyRun,
    *,
    error: Exception | str,
    completed_at: datetime | None = None,
) -> ProductCopyRun:
    _require_running(run)
    run.status = ExtractionRunStatus.FAILED
    run.generated_text = None
    run.usage = None
    run.sanitized_error = sanitize_extraction_error(error)
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def is_product_copy_run_stale(session: Session, run: ProductCopyRun) -> bool:
    current = build_product_copy_input_snapshot(session, run.product_id)
    return (
        build_product_copy_source_fingerprint(current)
        != run.source_fingerprint.lower()
    )


def _require_running(run: ProductCopyRun) -> None:
    if run.status is not ExtractionRunStatus.RUNNING:
        raise CompletedProductCopyRunError(
            f"Product copy run is already complete: {run.id}"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
