import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Brand,
    ExtractionFieldReview,
    ExtractionRun,
    Product,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import (
    ExtractionReviewDecision,
    ExtractionReviewField,
    ExtractionRunStatus,
    FieldSource,
    FieldState,
    SKUFieldName,
)
from app.domain.schemas import (
    ExtractionFieldReviewRead,
    ExtractionFieldReviewRequest,
    ProductExtractionResult,
)
from app.services.extraction_review import (
    DuplicateExtractionFieldReviewError,
    InvalidAcceptedObservationError,
    UnknownReviewExtractionRunError,
    UnknownReviewSKUError,
    UnreviewableExtractionRunError,
    apply_extraction_field_review,
)
from app.services.sku_field_provenance import (
    LockedSKUFieldError,
    apply_sku_field_update,
)


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'review.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_sku(session: Session, **values) -> SKU:
    sku = SKU(
        product=Product(name="Premium Whey", brand=Brand(name="Test Brand")),
        **values,
    )
    session.add(sku)
    session.flush()
    return sku


def observation(value, *, state="extracted", confidence="0.900000", evidence=None):
    return {
        "value": value,
        "confidence": confidence if value is not None else None,
        "evidence": evidence,
        "state": state,
    }


def extraction_result(**overrides) -> ProductExtractionResult:
    values = {
        "brand_name": observation("Test Brand"),
        "product_name": observation("Premium Whey"),
        "flavor": observation(
            "Vanilla", confidence="0.930000", evidence="Front label"
        ),
        "size_value": observation(
            2.0, confidence="0.880000", evidence="Net weight"
        ),
        "size_unit": observation(
            "lb", confidence="0.870000", evidence="Net weight unit"
        ),
        "servings": observation(
            30, confidence="0.840000", evidence="Nutrition panel"
        ),
        **overrides,
    }
    return ProductExtractionResult.model_validate(values)


def make_run(
    session: Session,
    *,
    status: ExtractionRunStatus = ExtractionRunStatus.SUCCEEDED,
    result: ProductExtractionResult | None = None,
) -> ExtractionRun:
    run = ExtractionRun(
        provider="test-provider",
        model="vision-model",
        prompt_version="product-extraction-v1",
        schema_version="product-result-v1",
        parameters_hash="a" * 64,
        status=status,
        started_at=NOW,
        completed_at=NOW if status is not ExtractionRunStatus.RUNNING else None,
        structured_result=(
            (result or extraction_result()).model_dump(mode="json")
            if status is ExtractionRunStatus.SUCCEEDED
            else None
        ),
        sanitized_error="failed" if status is ExtractionRunStatus.FAILED else None,
    )
    session.add(run)
    session.flush()
    return run


def review_request(
    run: ExtractionRun,
    sku: SKU,
    field_key: ExtractionReviewField,
    decision: ExtractionReviewDecision,
    **values,
) -> ExtractionFieldReviewRequest:
    return ExtractionFieldReviewRequest(
        extraction_run_id=run.id,
        sku_id=sku.id,
        field_key=field_key,
        decision=decision,
        **values,
    )


def provenance_for(sku: SKU, field_name: SKUFieldName):
    return next(
        item for item in sku.field_provenance if item.field_name is field_name
    )


def test_accept_extracted_flavor_applies_model_value_and_exact_lineage(
    session: Session,
) -> None:
    sku = make_sku(session)
    run = make_run(session)
    original_result = dict(run.structured_result)

    review = apply_extraction_field_review(
        session,
        review_request(
            run,
            sku,
            ExtractionReviewField.FLAVOR,
            ExtractionReviewDecision.ACCEPTED,
        ),
        applied_at=NOW,
    )
    provenance = provenance_for(sku, SKUFieldName.FLAVOR)

    assert sku.flavor == "Vanilla"
    assert provenance.source is FieldSource.MODEL
    assert provenance.state is FieldState.VERIFIED
    assert provenance.locked is True
    assert provenance.confidence == Decimal("0.930000")
    assert provenance.evidence == "Front label"
    assert provenance.extraction_run_id == run.id
    assert review.applied_at == NOW
    assert review.corrected_value is None
    assert review in run.field_reviews
    assert review in sku.extraction_field_reviews
    assert run.structured_result == original_result


def test_correct_flavor_applies_human_value_without_model_lineage(
    session: Session,
) -> None:
    sku = make_sku(session)
    run = make_run(session)

    review = apply_extraction_field_review(
        session,
        review_request(
            run,
            sku,
            ExtractionReviewField.FLAVOR,
            ExtractionReviewDecision.CORRECTED,
            corrected_value={"flavor": " Chocolate "},
        ),
    )
    provenance = provenance_for(sku, SKUFieldName.FLAVOR)

    assert sku.flavor == "Chocolate"
    assert provenance.source is FieldSource.HUMAN
    assert provenance.state is FieldState.VERIFIED
    assert provenance.locked is True
    assert provenance.confidence is None
    assert provenance.extraction_run_id is None
    assert review.corrected_value == {"flavor": "Chocolate"}
    assert ExtractionFieldReviewRead.model_validate(review).decision is (
        ExtractionReviewDecision.CORRECTED
    )


def test_reject_flavor_is_audit_only(session: Session) -> None:
    sku = make_sku(session, flavor="Chocolate")
    existing = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.FLAVOR,
        value="Chocolate",
        source=FieldSource.HUMAN,
        evidence="Previously reviewed",
    )
    session.flush()
    run = make_run(session)

    review = apply_extraction_field_review(
        session,
        review_request(
            run,
            sku,
            ExtractionReviewField.FLAVOR,
            ExtractionReviewDecision.REJECTED,
        ),
    )

    assert review.applied_at is None
    assert sku.flavor == "Chocolate"
    assert provenance_for(sku, SKUFieldName.FLAVOR) is existing
    assert existing.evidence == "Previously reviewed"
    assert len(sku.field_provenance) == 1


def test_accept_and_correct_servings(session: Session) -> None:
    accepted_sku = make_sku(session)
    accepted_run = make_run(session)
    apply_extraction_field_review(
        session,
        review_request(
            accepted_run,
            accepted_sku,
            ExtractionReviewField.SERVINGS,
            ExtractionReviewDecision.ACCEPTED,
        ),
    )

    corrected_sku = make_sku(session)
    corrected_run = make_run(session)
    apply_extraction_field_review(
        session,
        review_request(
            corrected_run,
            corrected_sku,
            ExtractionReviewField.SERVINGS,
            ExtractionReviewDecision.CORRECTED,
            corrected_value={"servings": 24},
        ),
    )

    assert accepted_sku.servings == 30
    assert provenance_for(accepted_sku, SKUFieldName.SERVINGS).source is (
        FieldSource.MODEL
    )
    assert corrected_sku.servings == 24
    assert provenance_for(corrected_sku, SKUFieldName.SERVINGS).source is (
        FieldSource.HUMAN
    )


@pytest.mark.parametrize("state", ["not_present", "not_legible"])
def test_absent_or_unreadable_servings_cannot_clear_existing_value(
    session: Session,
    state: str,
) -> None:
    sku = make_sku(session, servings=30)
    existing = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.SERVINGS,
        value=30,
        source=FieldSource.HUMAN,
    )
    session.flush()
    run = make_run(
        session,
        result=extraction_result(servings=observation(None, state=state)),
    )

    with pytest.raises(InvalidAcceptedObservationError):
        apply_extraction_field_review(
            session,
            review_request(
                run,
                sku,
                ExtractionReviewField.SERVINGS,
                ExtractionReviewDecision.ACCEPTED,
            ),
        )

    assert sku.servings == 30
    assert provenance_for(sku, SKUFieldName.SERVINGS) is existing
    assert run.field_reviews == []


def test_accept_complete_size_updates_both_fields_and_provenance(
    session: Session,
) -> None:
    sku = make_sku(session)
    run = make_run(session)

    review = apply_extraction_field_review(
        session,
        review_request(
            run,
            sku,
            ExtractionReviewField.SIZE,
            ExtractionReviewDecision.ACCEPTED,
        ),
    )

    assert sku.size_value == Decimal("2.000000")
    assert sku.size_unit == "lb"
    assert review.field_key is ExtractionReviewField.SIZE
    assert {
        item.field_name for item in sku.field_provenance
    } == {SKUFieldName.SIZE_VALUE, SKUFieldName.SIZE_UNIT}
    for provenance in sku.field_provenance:
        assert provenance.source is FieldSource.MODEL
        assert provenance.state is FieldState.VERIFIED
        assert provenance.locked is True
        assert provenance.extraction_run_id == run.id


def test_incomplete_extracted_size_cannot_be_accepted(session: Session) -> None:
    sku = make_sku(session)
    run = make_run(
        session,
        result=extraction_result(
            size_unit=observation(None, state="not_present"),
        ),
    )

    with pytest.raises(InvalidAcceptedObservationError):
        apply_extraction_field_review(
            session,
            review_request(
                run,
                sku,
                ExtractionReviewField.SIZE,
                ExtractionReviewDecision.ACCEPTED,
            ),
        )

    assert sku.size_value is None
    assert sku.size_unit is None
    assert sku.field_provenance == []
    assert run.field_reviews == []


def test_correct_complete_size_applies_decimal_pair(session: Session) -> None:
    sku = make_sku(session)
    run = make_run(session)

    review = apply_extraction_field_review(
        session,
        review_request(
            run,
            sku,
            ExtractionReviewField.SIZE,
            ExtractionReviewDecision.CORRECTED,
            corrected_value={"size_value": "2.500000", "size_unit": " kg "},
        ),
    )

    assert sku.size_value == Decimal("2.500000")
    assert sku.size_unit == "kg"
    assert review.corrected_value == {"size_value": "2.500000", "size_unit": "kg"}
    for provenance in sku.field_provenance:
        assert provenance.source is FieldSource.HUMAN
        assert provenance.locked is True
        assert provenance.extraction_run_id is None


@pytest.mark.parametrize(
    "field_key, corrected_value",
    [
        ("size", {"size_value": "2.5"}),
        ("size", {"size_value": "1.0000001", "size_unit": "kg"}),
        ("servings", {"servings": "30"}),
        ("flavor", {"flavor": ""}),
    ],
)
def test_typed_correction_contract_rejects_invalid_values(
    field_key: str,
    corrected_value: dict,
) -> None:
    with pytest.raises(ValidationError):
        ExtractionFieldReviewRequest(
            extraction_run_id=uuid.uuid4(),
            sku_id=uuid.uuid4(),
            field_key=field_key,
            decision="corrected",
            corrected_value=corrected_value,
        )


def test_review_contract_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExtractionFieldReviewRequest.model_validate(
            {
                "extraction_run_id": str(uuid.uuid4()),
                "sku_id": str(uuid.uuid4()),
                "field_key": "flavor",
                "decision": "accepted",
                "unexpected": True,
            }
        )


def test_failed_size_review_leaves_pair_and_both_provenance_rows_unchanged(
    session: Session,
) -> None:
    sku = make_sku(session, size_value=Decimal("1.000000"), size_unit="kg")
    value_provenance = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.SIZE_VALUE,
        value=Decimal("1.000000"),
        source=FieldSource.RULE,
    )
    unit_provenance = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.SIZE_UNIT,
        value="kg",
        source=FieldSource.HUMAN,
    )
    session.flush()
    run = make_run(session)

    with pytest.raises(LockedSKUFieldError):
        apply_extraction_field_review(
            session,
            review_request(
                run,
                sku,
                ExtractionReviewField.SIZE,
                ExtractionReviewDecision.ACCEPTED,
            ),
        )

    assert (sku.size_value, sku.size_unit) == (Decimal("1.000000"), "kg")
    assert value_provenance.source is FieldSource.RULE
    assert value_provenance.locked is False
    assert unit_provenance.source is FieldSource.HUMAN
    assert unit_provenance.locked is True
    assert run.field_reviews == []


def test_locked_field_requires_explicit_human_replace_flag(session: Session) -> None:
    sku = make_sku(session)
    existing = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.FLAVOR,
        value="Chocolate",
        source=FieldSource.HUMAN,
    )
    session.flush()
    run = make_run(session)
    request = review_request(
        run,
        sku,
        ExtractionReviewField.FLAVOR,
        ExtractionReviewDecision.ACCEPTED,
    )

    with pytest.raises(LockedSKUFieldError):
        apply_extraction_field_review(session, request)
    review = apply_extraction_field_review(
        session,
        request.model_copy(update={"replace_locked": True}),
    )

    assert review.applied_at is not None
    assert sku.flavor == "Vanilla"
    assert existing.source is FieldSource.MODEL
    assert existing.locked is True
    assert existing.extraction_run_id == run.id


def test_automated_update_still_cannot_replace_reviewed_lock(session: Session) -> None:
    sku = make_sku(session)
    run = make_run(session)
    apply_extraction_field_review(
        session,
        review_request(
            run,
            sku,
            ExtractionReviewField.FLAVOR,
            ExtractionReviewDecision.ACCEPTED,
        ),
    )

    with pytest.raises(LockedSKUFieldError):
        apply_sku_field_update(
            session,
            sku=sku,
            field_name=SKUFieldName.FLAVOR,
            value="Strawberry",
            source=FieldSource.MODEL,
        )

    assert sku.flavor == "Vanilla"


@pytest.mark.parametrize(
    "status", [ExtractionRunStatus.RUNNING, ExtractionRunStatus.FAILED]
)
def test_only_succeeded_extraction_runs_are_reviewable(
    session: Session,
    status: ExtractionRunStatus,
) -> None:
    sku = make_sku(session)
    run = make_run(session, status=status)

    with pytest.raises(UnreviewableExtractionRunError):
        apply_extraction_field_review(
            session,
            review_request(
                run,
                sku,
                ExtractionReviewField.FLAVOR,
                ExtractionReviewDecision.REJECTED,
            ),
        )


def test_review_requires_existing_run_and_sku(session: Session) -> None:
    sku = make_sku(session)
    run = make_run(session)
    with pytest.raises(UnknownReviewExtractionRunError):
        apply_extraction_field_review(
            session,
            ExtractionFieldReviewRequest(
                extraction_run_id=uuid.uuid4(),
                sku_id=sku.id,
                field_key="flavor",
                decision="rejected",
            ),
        )
    with pytest.raises(UnknownReviewSKUError):
        apply_extraction_field_review(
            session,
            ExtractionFieldReviewRequest(
                extraction_run_id=run.id,
                sku_id=uuid.uuid4(),
                field_key="flavor",
                decision="rejected",
            ),
        )


def test_duplicate_review_cannot_rewrite_completed_decision(session: Session) -> None:
    sku = make_sku(session)
    run = make_run(session)
    original = apply_extraction_field_review(
        session,
        review_request(
            run,
            sku,
            ExtractionReviewField.FLAVOR,
            ExtractionReviewDecision.REJECTED,
        ),
    )

    with pytest.raises(DuplicateExtractionFieldReviewError):
        apply_extraction_field_review(
            session,
            review_request(
                run,
                sku,
                ExtractionReviewField.FLAVOR,
                ExtractionReviewDecision.CORRECTED,
                corrected_value={"flavor": "Chocolate"},
            ),
        )

    assert session.query(ExtractionFieldReview).count() == 1
    assert original.decision is ExtractionReviewDecision.REJECTED
    assert original.corrected_value is None
    assert sku.flavor is None
