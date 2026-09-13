from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExtractionFieldReview, ExtractionRun, SKU, SKUFieldProvenance
from app.db.types import utc_now
from app.domain.enums import (
    ExtractionReviewDecision,
    ExtractionReviewField,
    ExtractionRunStatus,
    FieldSource,
    ObservationState,
    SKUFieldName,
)
from app.domain.schemas import (
    ExtractionFieldReviewRequest,
    FieldObservation,
    FlavorCorrection,
    ProductExtractionResult,
    ServingsCorrection,
    SizeCorrection,
)
from app.services.sku_field_provenance import (
    LockedSKUFieldError,
    apply_reviewed_sku_field_update,
)


class ExtractionReviewError(ValueError):
    pass


class UnknownReviewExtractionRunError(ExtractionReviewError):
    pass


class UnreviewableExtractionRunError(ExtractionReviewError):
    pass


class UnknownReviewSKUError(ExtractionReviewError):
    pass


class DuplicateExtractionFieldReviewError(ExtractionReviewError):
    pass


class InvalidAcceptedObservationError(ExtractionReviewError):
    pass


ReviewValue = str | Decimal | int
ReviewUpdate = tuple[SKUFieldName, ReviewValue, Decimal | None, str | None]


def apply_extraction_field_review(
    session: Session,
    request: ExtractionFieldReviewRequest | Mapping[str, Any],
    *,
    applied_at: datetime | None = None,
) -> ExtractionFieldReview:
    """Persist and, when applicable, atomically apply one human review decision."""

    validated = ExtractionFieldReviewRequest.model_validate(request)
    run = session.get(ExtractionRun, validated.extraction_run_id)
    if run is None:
        raise UnknownReviewExtractionRunError(
            f"extraction run not found: {validated.extraction_run_id}"
        )
    if run.status is not ExtractionRunStatus.SUCCEEDED:
        raise UnreviewableExtractionRunError(
            f"extraction run must be succeeded: {run.id}"
        )
    sku = session.get(SKU, validated.sku_id)
    if sku is None:
        raise UnknownReviewSKUError(f"SKU not found: {validated.sku_id}")

    existing = session.scalar(
        select(ExtractionFieldReview).where(
            ExtractionFieldReview.extraction_run_id == run.id,
            ExtractionFieldReview.sku_id == sku.id,
            ExtractionFieldReview.field_key == validated.field_key,
        )
    )
    if existing is not None:
        raise DuplicateExtractionFieldReviewError(
            "a review already exists for this extraction run, SKU and field"
        )

    if validated.decision is ExtractionReviewDecision.REJECTED:
        review = _build_review(validated, run, sku)
        session.add(review)
        session.flush()
        return review

    updates = _resolve_updates(validated, run)
    field_names = [field_name for field_name, _, _, _ in updates]
    _require_replaceable_fields(
        session,
        sku,
        field_names,
        replace_locked=validated.replace_locked,
    )

    review = _build_review(validated, run, sku)
    session.add(review)
    source = (
        FieldSource.MODEL
        if validated.decision is ExtractionReviewDecision.ACCEPTED
        else FieldSource.HUMAN
    )
    for field_name, value, confidence, evidence in updates:
        apply_reviewed_sku_field_update(
            session,
            sku=sku,
            field_name=field_name,
            value=value,
            source=source,
            confidence=confidence,
            evidence=evidence,
            extraction_run=(run if source is FieldSource.MODEL else None),
            replace_locked=validated.replace_locked,
        )

    review.applied_at = applied_at or utc_now()
    session.flush()
    return review


def _resolve_updates(
    request: ExtractionFieldReviewRequest,
    run: ExtractionRun,
) -> list[ReviewUpdate]:
    if request.decision is ExtractionReviewDecision.CORRECTED:
        correction = request.corrected_value
        if isinstance(correction, FlavorCorrection):
            return [(SKUFieldName.FLAVOR, correction.flavor, None, None)]
        if isinstance(correction, ServingsCorrection):
            return [(SKUFieldName.SERVINGS, correction.servings, None, None)]
        if isinstance(correction, SizeCorrection):
            return [
                (SKUFieldName.SIZE_VALUE, correction.size_value, None, None),
                (SKUFieldName.SIZE_UNIT, correction.size_unit, None, None),
            ]
        raise ExtractionReviewError("corrected review is missing its typed value")

    try:
        result = ProductExtractionResult.model_validate(run.structured_result)
    except ValidationError as error:
        raise UnreviewableExtractionRunError(
            "successful extraction run has invalid structured output"
        ) from error

    if request.field_key is ExtractionReviewField.FLAVOR:
        return [_accepted_observation(SKUFieldName.FLAVOR, result.flavor)]
    if request.field_key is ExtractionReviewField.SERVINGS:
        return [_accepted_observation(SKUFieldName.SERVINGS, result.servings)]
    return [
        _accepted_observation(SKUFieldName.SIZE_VALUE, result.size_value),
        _accepted_observation(SKUFieldName.SIZE_UNIT, result.size_unit),
    ]


def _accepted_observation(
    field_name: SKUFieldName,
    observation: FieldObservation[Any],
) -> ReviewUpdate:
    if observation.state is not ObservationState.EXTRACTED or observation.value is None:
        raise InvalidAcceptedObservationError(
            f"cannot accept {field_name.value}: model observation is not extracted"
        )
    return (
        field_name,
        observation.value,
        observation.confidence,
        observation.evidence,
    )


def _build_review(
    request: ExtractionFieldReviewRequest,
    run: ExtractionRun,
    sku: SKU,
) -> ExtractionFieldReview:
    return ExtractionFieldReview(
        extraction_run=run,
        sku=sku,
        field_key=request.field_key,
        decision=request.decision,
        corrected_value=(
            request.corrected_value.model_dump(mode="json")
            if request.corrected_value is not None
            else None
        ),
    )


def _require_replaceable_fields(
    session: Session,
    sku: SKU,
    field_names: list[SKUFieldName],
    *,
    replace_locked: bool,
) -> None:
    if replace_locked:
        return
    with session.no_autoflush:
        provenance = session.scalars(
            select(SKUFieldProvenance).where(
                SKUFieldProvenance.sku_id == sku.id,
                SKUFieldProvenance.field_name.in_(field_names),
            )
        ).all()
    locked_fields = sorted(
        record.field_name.value for record in provenance if record.locked
    )
    if locked_fields:
        raise LockedSKUFieldError(
            "cannot replace locked field without explicit approval: "
            + ", ".join(locked_fields)
        )
