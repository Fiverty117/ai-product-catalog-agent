import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.category_suggestion import (
    OPENAI_CATEGORY_PROVIDER,
    configured_openai_category_model,
    normalize_category_parameters,
)
from app.ai.prompts.product_category_v1 import PROMPT_VERSION
from app.db.models import CategorySuggestionRun, Job, Product, SKU
from app.db.types import utc_now
from app.domain.enums import ExtractionRunStatus
from app.domain.schemas import (
    CategorySuggestionBrandSnapshot,
    CategorySuggestionInputSnapshot,
    CategorySuggestionJobPayload,
    CategorySuggestionProductSnapshot,
    CategorySuggestionResult,
    CategorySuggestionSKUSnapshot,
    CategoryTaxonomySnapshot,
)
from app.services.categories import list_active_categories
from app.services.extraction import sanitize_extraction_error
from app.services.jobs import enqueue_job

PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE = "product.category_suggest.v1"
PRODUCT_CATEGORY_SCHEMA_VERSION = "product-category-result-v1"


class CategorySuggestionRunError(ValueError):
    pass


class UnknownCategorySuggestionProductError(CategorySuggestionRunError):
    pass


class InvalidCategorySuggestionJobError(CategorySuggestionRunError):
    pass


class CompletedCategorySuggestionRunError(CategorySuggestionRunError):
    pass


def build_category_suggestion_input_snapshot(
    session: Session,
    product_id: uuid.UUID,
) -> CategorySuggestionInputSnapshot:
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownCategorySuggestionProductError(
            f"Product not found: {product_id}"
        )
    skus = session.scalars(
        select(SKU).where(SKU.product_id == product.id).order_by(SKU.id)
    ).all()
    taxonomy = list_active_categories(session)
    return CategorySuggestionInputSnapshot(
        product=CategorySuggestionProductSnapshot(
            product_id=product.id,
            product_name=product.name,
        ),
        brand=CategorySuggestionBrandSnapshot(
            brand_id=product.brand.id,
            brand_name=product.brand.name,
        ),
        sku_variants=[
            CategorySuggestionSKUSnapshot(
                sku_id=sku.id,
                flavor=sku.flavor,
                size_value=sku.size_value,
                size_unit=sku.size_unit,
                servings=sku.servings,
            )
            for sku in skus
        ],
        taxonomy=[
            CategoryTaxonomySnapshot(
                category_id=category.id,
                name=category.name,
                identity_key=category.identity_key,
                sort_order=category.sort_order,
            )
            for category in taxonomy
        ],
    )


def build_category_suggestion_input_hash(
    *,
    input_snapshot: CategorySuggestionInputSnapshot,
    provider: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    parameters: Mapping[str, object],
) -> str:
    identity = {
        "job_type": PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE,
        "input_snapshot": input_snapshot.model_dump(mode="json", exclude_none=True),
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "parameters": dict(parameters),
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def build_category_suggestion_idempotency_key(
    payload: CategorySuggestionJobPayload,
) -> str:
    expected_hash = build_category_suggestion_input_hash(
        input_snapshot=payload.input_snapshot,
        provider=payload.provider,
        model=payload.model,
        prompt_version=payload.prompt_version,
        schema_version=payload.schema_version,
        parameters=payload.parameters,
    )
    if payload.input_hash.lower() != expected_hash:
        raise ValueError("category suggestion input_hash does not match its payload")
    return f"{PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE}:{expected_hash}"


def enqueue_category_suggestion(
    session: Session,
    *,
    product_id: uuid.UUID,
    provider: str = OPENAI_CATEGORY_PROVIDER,
    model: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = PRODUCT_CATEGORY_SCHEMA_VERSION,
    parameters: Mapping[str, object] | None = None,
    max_attempts: int = 3,
) -> Job:
    """Build a trusted canonical snapshot and enqueue its logical request."""

    resolved_parameters = normalize_category_parameters(parameters)
    resolved_model = model or configured_openai_category_model()
    snapshot = build_category_suggestion_input_snapshot(session, product_id)
    input_hash = build_category_suggestion_input_hash(
        input_snapshot=snapshot,
        provider=provider,
        model=resolved_model,
        prompt_version=prompt_version,
        schema_version=schema_version,
        parameters=resolved_parameters,
    )
    payload = CategorySuggestionJobPayload(
        product_id=product_id,
        provider=provider,
        model=resolved_model,
        prompt_version=prompt_version,
        schema_version=schema_version,
        parameters=resolved_parameters,
        input_hash=input_hash,
        input_snapshot=snapshot,
    )
    return enqueue_job(
        session,
        job_type=PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE,
        payload=payload.model_dump(mode="json", exclude_none=True),
        idempotency_key=build_category_suggestion_idempotency_key(payload),
        max_attempts=max_attempts,
    )


def create_running_category_suggestion_run(
    session: Session,
    *,
    payload: CategorySuggestionJobPayload,
    job_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
) -> CategorySuggestionRun:
    if payload.product_id != payload.input_snapshot.product.product_id:
        raise InvalidCategorySuggestionJobError(
            "job Product does not match its input snapshot"
        )
    build_category_suggestion_idempotency_key(payload)
    product = session.get(Product, payload.product_id)
    if product is None:
        raise UnknownCategorySuggestionProductError(
            f"Product not found: {payload.product_id}"
        )
    job = None
    if job_id is not None:
        job = session.get(Job, job_id)
        if job is None or job.job_type != PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE:
            raise InvalidCategorySuggestionJobError(
                f"job must exist with type {PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE}"
            )
    run = CategorySuggestionRun(
        product=product,
        job=job,
        provider=payload.provider,
        model=payload.model,
        prompt_version=payload.prompt_version,
        schema_version=payload.schema_version,
        parameters=payload.parameters,
        input_hash=payload.input_hash.lower(),
        input_snapshot=payload.input_snapshot.model_dump(
            mode="json", exclude_none=True
        ),
        status=ExtractionRunStatus.RUNNING,
        started_at=started_at or utc_now(),
    )
    session.add(run)
    session.flush()
    return run


def is_category_suggestion_run_stale(
    session: Session,
    run: CategorySuggestionRun,
) -> bool:
    """Derive freshness from current canonical context and the original hash inputs."""

    current_snapshot = build_category_suggestion_input_snapshot(
        session,
        run.product_id,
    )
    normalized_parameters = normalize_category_parameters(run.parameters)
    current_hash = build_category_suggestion_input_hash(
        input_snapshot=current_snapshot,
        provider=run.provider,
        model=run.model,
        prompt_version=run.prompt_version,
        schema_version=run.schema_version,
        parameters=normalized_parameters,
    )
    return current_hash != run.input_hash.lower()


def mark_category_suggestion_run_succeeded(
    session: Session,
    run: CategorySuggestionRun,
    *,
    structured_result: CategorySuggestionResult | Mapping[str, Any],
    usage: Mapping[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> CategorySuggestionRun:
    _require_running(run)
    snapshot = CategorySuggestionInputSnapshot.model_validate(run.input_snapshot)
    taxonomy_ids = {item.category_id for item in snapshot.taxonomy}
    result = CategorySuggestionResult.model_validate(
        structured_result,
        context={"taxonomy_ids": taxonomy_ids},
    )
    run.status = ExtractionRunStatus.SUCCEEDED
    run.structured_result = result.model_dump(mode="json")
    run.usage = dict(usage) if usage is not None else None
    run.sanitized_error = None
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def mark_category_suggestion_run_failed(
    session: Session,
    run: CategorySuggestionRun,
    *,
    error: Exception | str,
    completed_at: datetime | None = None,
) -> CategorySuggestionRun:
    _require_running(run)
    run.status = ExtractionRunStatus.FAILED
    run.structured_result = None
    run.usage = None
    run.sanitized_error = sanitize_extraction_error(error)
    run.completed_at = completed_at or utc_now()
    session.flush()
    return run


def _require_running(run: CategorySuggestionRun) -> None:
    if run.status is not ExtractionRunStatus.RUNNING:
        raise CompletedCategorySuggestionRunError(
            f"category suggestion run is already complete: {run.id}"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
