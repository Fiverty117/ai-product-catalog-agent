import uuid
from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Brand,
    CategorySuggestionReview,
    Product,
    ProductCategory,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import (
    CategorySuggestionReviewDecision,
    ExtractionRunStatus,
    FieldSource,
)
from app.domain.schemas import CategorySuggestionReviewRequest
from app.services.categories import (
    PrimaryCategoryConflictError,
    assign_product_category,
    create_category,
    set_category_active,
)
from app.services.category_suggestion_review import (
    DuplicateCategorySuggestionReviewError,
    StaleCategorySuggestionRunError,
    UnavailableReviewCategoryError,
    UnreviewableCategorySuggestionRunError,
    apply_category_suggestion_review,
)
from app.services.category_suggestions import (
    build_category_suggestion_input_snapshot,
    create_running_category_suggestion_run,
    mark_category_suggestion_run_failed,
    mark_category_suggestion_run_succeeded,
)
from app.domain.schemas import CategorySuggestionJobPayload
from app.services.category_suggestions import build_category_suggestion_input_hash


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'review.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def setup(session: Session):
    product = Product(name="Maca", brand=Brand(name="Brand"))
    session.add(product)
    primary = create_category(session, name="Superfoods")
    secondary = create_category(session, name="Adaptogens")
    unrelated = create_category(session, name="Organic")
    snapshot = build_category_suggestion_input_snapshot(session, product.id)
    values = {
        "product_id": product.id,
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "prompt_version": "product-category-v1",
        "schema_version": "product-category-result-v1",
        "parameters": {"reasoning_effort": "low"},
        "input_snapshot": snapshot,
    }
    values["input_hash"] = build_category_suggestion_input_hash(
        input_snapshot=snapshot,
        provider=values["provider"],
        model=values["model"],
        prompt_version=values["prompt_version"],
        schema_version=values["schema_version"],
        parameters=values["parameters"],
    )
    run = create_running_category_suggestion_run(
        session, payload=CategorySuggestionJobPayload.model_validate(values)
    )
    return product, primary, secondary, unrelated, run


def suggestion(primary, secondary=()):
    return {
        "primary": (
            {
                "category_id": primary.id,
                "confidence": "0.95",
                "evidence": "Canonical name supports this category.",
            }
            if primary is not None
            else None
        ),
        "secondary": [
            {
                "category_id": category.id,
                "confidence": "0.7",
                "evidence": "Canonical context supports this category.",
            }
            for category in secondary
        ],
    }


def complete(run, session, primary, secondary=()):
    mark_category_suggestion_run_succeeded(
        session, run, structured_result=suggestion(primary, secondary)
    )


def review(run, decision, **kwargs):
    return CategorySuggestionReviewRequest(
        category_suggestion_run_id=run.id,
        decision=decision,
        **kwargs,
    )


def test_accepted_bundle_adds_locked_model_assignments_with_lineage(
    session: Session,
) -> None:
    product, primary, secondary, _, run = setup(session)
    complete(run, session, primary, [secondary])

    audit = apply_category_suggestion_review(
        session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
    )
    assignments = {item.category_id: item for item in product.category_assignments}

    assert assignments[primary.id].is_primary is True
    assert assignments[secondary.id].is_primary is False
    assert all(item.source is FieldSource.MODEL for item in assignments.values())
    assert all(item.verified and item.locked for item in assignments.values())
    assert all(
        item.category_suggestion_run_id == run.id for item in assignments.values()
    )
    assert audit.applied_at is not None
    assert audit.final_selection is None


def test_corrected_bundle_is_human_without_model_lineage(session: Session) -> None:
    product, suggested, corrected, _, run = setup(session)
    complete(run, session, suggested)

    audit = apply_category_suggestion_review(
        session,
        review(
            run,
            CategorySuggestionReviewDecision.CORRECTED,
            corrected_selection={
                "primary_category_id": corrected.id,
                "secondary_category_ids": [],
            },
        ),
    )
    assignment = product.category_assignments[0]

    assert assignment.category_id == corrected.id
    assert assignment.source is FieldSource.HUMAN
    assert assignment.category_suggestion_run_id is None
    assert audit.final_selection["primary_category_id"] == str(corrected.id)


def test_rejected_review_is_audit_only(session: Session) -> None:
    _, primary, _, _, run = setup(session)
    complete(run, session, primary)

    audit = apply_category_suggestion_review(
        session, review(run, CategorySuggestionReviewDecision.REJECTED)
    )

    assert audit.applied_at is None
    assert session.scalar(select(ProductCategory)) is None


@pytest.mark.parametrize(
    "status", [ExtractionRunStatus.RUNNING, ExtractionRunStatus.FAILED]
)
def test_only_successful_runs_can_be_reviewed(session: Session, status) -> None:
    _, primary, _, _, run = setup(session)
    if status is ExtractionRunStatus.FAILED:
        mark_category_suggestion_run_failed(session, run, error="failed")

    with pytest.raises(UnreviewableCategorySuggestionRunError):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.REJECTED)
        )


def test_review_is_unique_and_immutable(session: Session) -> None:
    _, primary, _, _, run = setup(session)
    complete(run, session, primary)
    first = apply_category_suggestion_review(
        session, review(run, CategorySuggestionReviewDecision.REJECTED)
    )

    with pytest.raises(DuplicateCategorySuggestionReviewError):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )
    assert first.decision is CategorySuggestionReviewDecision.REJECTED


def test_deactivated_historical_suggestion_cannot_be_applied(
    session: Session,
) -> None:
    _, primary, _, _, run = setup(session)
    complete(run, session, primary)
    historical_snapshot = deepcopy(run.input_snapshot)
    historical_hash = run.input_hash
    set_category_active(session, category_id=primary.id, is_active=False)

    with pytest.raises(StaleCategorySuggestionRunError, match="stale"):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )
    assert run.input_snapshot == historical_snapshot
    assert run.input_hash == historical_hash
    assert session.scalar(select(CategorySuggestionReview)) is None
    assert session.scalar(select(ProductCategory)) is None


def test_product_rename_makes_accepted_review_stale_without_side_effects(
    session: Session,
) -> None:
    product, primary, _, _, run = setup(session)
    complete(run, session, primary)
    historical_snapshot = deepcopy(run.input_snapshot)
    historical_hash = run.input_hash
    product.name = "Maca Premium"
    session.flush()

    with pytest.raises(StaleCategorySuggestionRunError, match="new suggestion"):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )

    assert session.scalar(select(CategorySuggestionReview)) is None
    assert session.scalar(select(ProductCategory)) is None
    assert run.input_snapshot == historical_snapshot
    assert run.input_hash == historical_hash


def test_relevant_sku_change_makes_accepted_review_stale(session: Session) -> None:
    product, primary, _, _, run = setup(session)
    complete(run, session, primary)
    session.add(SKU(product=product, flavor="Berry", servings=30))
    session.flush()

    with pytest.raises(StaleCategorySuggestionRunError, match="stale"):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )

    assert session.scalar(select(CategorySuggestionReview)) is None
    assert session.scalar(select(ProductCategory)) is None


def test_category_addition_makes_accepted_review_stale(session: Session) -> None:
    _, primary, _, _, run = setup(session)
    complete(run, session, primary)
    create_category(session, name="New taxonomy entry")

    with pytest.raises(StaleCategorySuggestionRunError, match="stale"):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )


def test_category_rename_makes_accepted_review_stale(session: Session) -> None:
    _, primary, _, _, run = setup(session)
    complete(run, session, primary)
    primary.name = "Super Foods"
    session.flush()

    with pytest.raises(StaleCategorySuggestionRunError, match="stale"):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )


def test_renamed_category_keeps_id_for_explicit_corrected_review(session: Session) -> None:
    product, primary, _, _, run = setup(session)
    complete(run, session, primary)
    original_id = primary.id
    primary.name = "Super Foods"
    session.flush()

    applied = apply_category_suggestion_review(
        session, review(run, CategorySuggestionReviewDecision.CORRECTED, corrected_selection={
            "primary_category_id": original_id, "secondary_category_ids": [],
        }),
    )
    assert applied.applied_at is not None
    assert session.scalar(select(ProductCategory).where(ProductCategory.product_id == product.id)).category_id == original_id


def test_pending_suggestion_cannot_apply_after_category_deactivation(session: Session) -> None:
    _, primary, _, _, run = setup(session)
    complete(run, session, primary)
    set_category_active(session, category_id=primary.id, is_active=False)

    with pytest.raises(StaleCategorySuggestionRunError, match="stale"):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )
    assert session.scalar(select(ProductCategory)) is None


def test_stale_corrected_review_allows_explicit_current_active_categories(
    session: Session,
) -> None:
    product, suggested, corrected, _, run = setup(session)
    complete(run, session, suggested)
    product.name = "Current canonical product name"
    session.flush()

    audit = apply_category_suggestion_review(
        session,
        review(
            run,
            CategorySuggestionReviewDecision.CORRECTED,
            corrected_selection={
                "primary_category_id": corrected.id,
                "secondary_category_ids": [],
            },
        ),
    )
    assignment = product.category_assignments[0]

    assert audit.applied_at is not None
    assert assignment.category_id == corrected.id
    assert assignment.source is FieldSource.HUMAN
    assert assignment.category_suggestion_run_id is None


def test_stale_rejected_review_remains_audit_only(session: Session) -> None:
    product, primary, _, _, run = setup(session)
    complete(run, session, primary)
    product.name = "Changed after inference"
    session.flush()

    audit = apply_category_suggestion_review(
        session, review(run, CategorySuggestionReviewDecision.REJECTED)
    )

    assert audit.applied_at is None
    assert session.scalar(select(ProductCategory)) is None


def test_corrected_selection_rejects_inactive_and_invalid_bundles(
    session: Session,
) -> None:
    _, primary, secondary, _, run = setup(session)
    complete(run, session, primary)
    set_category_active(session, category_id=secondary.id, is_active=False)

    with pytest.raises(UnavailableReviewCategoryError):
        apply_category_suggestion_review(
            session,
            review(
                run,
                CategorySuggestionReviewDecision.CORRECTED,
                corrected_selection={
                    "primary_category_id": secondary.id,
                    "secondary_category_ids": [],
                },
            ),
        )
    with pytest.raises(ValidationError, match="also be secondary"):
        review(
            run,
            CategorySuggestionReviewDecision.CORRECTED,
            corrected_selection={
                "primary_category_id": primary.id,
                "secondary_category_ids": [primary.id],
            },
        )


def test_existing_origin_and_unrelated_categories_are_preserved(
    session: Session,
) -> None:
    product, primary, secondary, unrelated, run = setup(session)
    existing = assign_product_category(
        session, product_id=product.id, category_id=primary.id, is_primary=True
    )
    other = assign_product_category(
        session, product_id=product.id, category_id=unrelated.id
    )
    complete(run, session, primary, [secondary])

    apply_category_suggestion_review(
        session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
    )

    assert existing.source is FieldSource.HUMAN
    assert existing.category_suggestion_run_id is None
    assert session.get(ProductCategory, other.id) is other
    added = next(
        item for item in product.category_assignments if item.category_id == secondary.id
    )
    assert added.source is FieldSource.MODEL
    assert len(product.category_assignments) == 3


def test_primary_conflict_requires_explicit_human_replacement(
    session: Session,
) -> None:
    product, old, new, unrelated, run = setup(session)
    old_assignment = assign_product_category(
        session, product_id=product.id, category_id=old.id, is_primary=True
    )
    unrelated_assignment = assign_product_category(
        session, product_id=product.id, category_id=unrelated.id
    )
    complete(run, session, new)

    with pytest.raises(PrimaryCategoryConflictError):
        apply_category_suggestion_review(
            session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
        )
    assert session.scalar(select(CategorySuggestionReview)) is None
    assert old_assignment.is_primary is True

    audit = apply_category_suggestion_review(
        session,
        review(
            run,
            CategorySuggestionReviewDecision.ACCEPTED,
            replace_primary=True,
        ),
    )
    new_assignment = next(
        item for item in product.category_assignments if item.category_id == new.id
    )
    assert audit.applied_at is not None
    assert old_assignment.is_primary is False
    assert new_assignment.is_primary is True
    assert session.get(ProductCategory, unrelated_assignment.id) is not None


def test_caller_rollback_removes_review_and_new_assignments(session: Session) -> None:
    product, primary, _, _, run = setup(session)
    complete(run, session, primary)
    session.commit()
    audit = apply_category_suggestion_review(
        session, review(run, CategorySuggestionReviewDecision.ACCEPTED)
    )

    session.rollback()

    assert session.get(CategorySuggestionReview, audit.id) is None
    assert session.scalar(
        select(func.count()).select_from(ProductCategory).where(
            ProductCategory.product_id == product.id
        )
    ) == 0


def test_failed_primary_replacement_rolls_back_completely(
    session: Session, monkeypatch
) -> None:
    product, old_category, new_category, _, run = setup(session)
    old = assign_product_category(
        session,
        product_id=product.id,
        category_id=old_category.id,
        is_primary=True,
    )
    complete(run, session, new_category)
    session.commit()
    real_flush = session.flush
    flush_calls = 0

    def fail_final_flush(*args, **kwargs):
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == 2:
            raise RuntimeError("canonical application failed")
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", fail_final_flush)
    with pytest.raises(RuntimeError, match="canonical application failed"):
        apply_category_suggestion_review(
            session,
            review(
                run,
                CategorySuggestionReviewDecision.ACCEPTED,
                replace_primary=True,
            ),
        )
    session.rollback()

    assert session.get(ProductCategory, old.id).is_primary is True
    assert session.scalar(select(CategorySuggestionReview)) is None
    assert session.scalar(
        select(ProductCategory).where(
            ProductCategory.category_id == new_category.id
        )
    ) is None
