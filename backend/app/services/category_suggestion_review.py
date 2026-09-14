import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Category,
    CategorySuggestionReview,
    CategorySuggestionRun,
    Product,
    ProductCategory,
)
from app.db.types import utc_now
from app.domain.enums import (
    CategorySuggestionReviewDecision,
    ExtractionRunStatus,
    FieldSource,
)
from app.domain.schemas import (
    CategorySelection,
    CategorySuggestionInputSnapshot,
    CategorySuggestionResult,
    CategorySuggestionReviewRequest,
)
from app.services.categories import PrimaryCategoryConflictError
from app.services.category_suggestions import is_category_suggestion_run_stale


class CategorySuggestionReviewError(ValueError):
    pass


class UnknownCategorySuggestionRunError(CategorySuggestionReviewError):
    pass


class UnreviewableCategorySuggestionRunError(CategorySuggestionReviewError):
    pass


class DuplicateCategorySuggestionReviewError(CategorySuggestionReviewError):
    pass


class UnknownReviewProductError(CategorySuggestionReviewError):
    pass


class InvalidCategorySuggestionResultError(CategorySuggestionReviewError):
    pass


class UnavailableReviewCategoryError(CategorySuggestionReviewError):
    pass


class StaleCategorySuggestionRunError(CategorySuggestionReviewError):
    pass


def apply_category_suggestion_review(
    session: Session,
    request: CategorySuggestionReviewRequest | Mapping[str, Any],
    *,
    applied_at: datetime | None = None,
) -> CategorySuggestionReview:
    """Audit one human bundle decision and atomically apply selected Categories."""

    validated = CategorySuggestionReviewRequest.model_validate(request)
    run = session.get(CategorySuggestionRun, validated.category_suggestion_run_id)
    if run is None:
        raise UnknownCategorySuggestionRunError(
            f"CategorySuggestionRun not found: {validated.category_suggestion_run_id}"
        )
    if run.status is not ExtractionRunStatus.SUCCEEDED:
        raise UnreviewableCategorySuggestionRunError(
            f"category suggestion run must be succeeded: {run.id}"
        )
    product = session.get(Product, run.product_id)
    if product is None:
        raise UnknownReviewProductError(f"Product not found: {run.product_id}")
    prior_review = session.scalar(
        select(CategorySuggestionReview).where(
            CategorySuggestionReview.category_suggestion_run_id == run.id
        )
    )
    if prior_review is not None:
        raise DuplicateCategorySuggestionReviewError(
            f"category suggestion run already reviewed: {run.id}"
        )

    if (
        validated.decision is CategorySuggestionReviewDecision.ACCEPTED
        and is_category_suggestion_run_stale(session, run)
    ):
        raise StaleCategorySuggestionRunError(
            "category suggestion is stale; request a new suggestion for current context"
        )

    if validated.decision is CategorySuggestionReviewDecision.REJECTED:
        review = CategorySuggestionReview(
            category_suggestion_run=run,
            decision=validated.decision,
            final_selection=None,
            applied_at=None,
        )
        session.add(review)
        session.flush()
        return review

    selection = _resolve_selection(validated, run)
    category_ids = [
        *(
            [selection.primary_category_id]
            if selection.primary_category_id is not None
            else []
        ),
        *selection.secondary_category_ids,
    ]
    categories = _require_active_categories(session, category_ids)
    assignments = session.scalars(
        select(ProductCategory).where(ProductCategory.product_id == product.id)
    ).all()
    assignments_by_category = {
        assignment.category_id: assignment for assignment in assignments
    }
    current_primary = next(
        (assignment for assignment in assignments if assignment.is_primary),
        None,
    )
    desired_primary = selection.primary_category_id
    if (
        desired_primary is not None
        and current_primary is not None
        and current_primary.category_id != desired_primary
        and not validated.replace_primary
    ):
        raise PrimaryCategoryConflictError(
            f"Product already has primary Category {current_primary.category_id}; "
            "set replace_primary=true for an explicit human replacement"
        )

    review = CategorySuggestionReview(
        category_suggestion_run=run,
        decision=validated.decision,
        final_selection=(
            selection.model_dump(mode="json")
            if validated.decision is CategorySuggestionReviewDecision.CORRECTED
            else None
        ),
    )
    session.add(review)
    source = (
        FieldSource.MODEL
        if validated.decision is CategorySuggestionReviewDecision.ACCEPTED
        else FieldSource.HUMAN
    )
    lineage = run if source is FieldSource.MODEL else None

    if (
        desired_primary is not None
        and current_primary is not None
        and current_primary.category_id != desired_primary
    ):
        current_primary.is_primary = False
        session.flush([current_primary])

    for category_id in category_ids:
        is_primary = category_id == desired_primary
        assignment = assignments_by_category.get(category_id)
        if assignment is None:
            assignment = ProductCategory(
                product=product,
                category=categories[category_id],
                is_primary=is_primary,
                source=source,
                verified=True,
                locked=True,
                category_suggestion_run=lineage,
            )
            session.add(assignment)
            assignments_by_category[category_id] = assignment
        elif is_primary:
            assignment.is_primary = True

    review.applied_at = applied_at or utc_now()
    session.flush()
    return review


def _resolve_selection(
    request: CategorySuggestionReviewRequest,
    run: CategorySuggestionRun,
) -> CategorySelection:
    if request.decision is CategorySuggestionReviewDecision.CORRECTED:
        if request.corrected_selection is None:
            raise CategorySuggestionReviewError("corrected selection is missing")
        return request.corrected_selection

    try:
        snapshot = CategorySuggestionInputSnapshot.model_validate(run.input_snapshot)
        taxonomy_ids = {item.category_id for item in snapshot.taxonomy}
        result = CategorySuggestionResult.model_validate(
            run.structured_result,
            context={"taxonomy_ids": taxonomy_ids},
        )
    except ValidationError:
        raise InvalidCategorySuggestionResultError(
            "successful category suggestion run has invalid structured output"
        ) from None
    return CategorySelection(
        primary_category_id=(
            result.primary.category_id if result.primary is not None else None
        ),
        secondary_category_ids=[item.category_id for item in result.secondary],
    )


def _require_active_categories(
    session: Session,
    category_ids: list[uuid.UUID],
) -> dict[uuid.UUID, Category]:
    if not category_ids:
        return {}
    categories = session.scalars(
        select(Category).where(Category.id.in_(category_ids))
    ).all()
    by_id = {category.id: category for category in categories}
    missing = [category_id for category_id in category_ids if category_id not in by_id]
    if missing:
        raise UnavailableReviewCategoryError(
            "selected Category does not currently exist: " + str(missing[0])
        )
    inactive = [category_id for category_id in category_ids if not by_id[category_id].is_active]
    if inactive:
        raise UnavailableReviewCategoryError(
            "selected Category is currently inactive: " + str(inactive[0])
        )
    return by_id
