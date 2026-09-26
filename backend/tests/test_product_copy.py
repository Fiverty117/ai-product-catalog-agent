import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Brand,
    Price,
    Product,
    ProductCopyReview,
    ProductCopyManualRevision,
    CatalogSnapshot,
    ProductCopyRun,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import (
    ExtractionRunStatus,
    ProductCopyResolutionState,
    ProductCopyReviewDecision,
)
from app.domain.schemas import (
    ProductCopyJobPayload,
    ProductCopyResult,
    ProductCopyReviewRequest,
    ProductCopyRunRead,
)
from app.services.categories import assign_product_category, create_category
from app.services.product_copy import (
    PRODUCT_COPY_SCHEMA_VERSION,
    build_product_copy_idempotency_key,
    build_product_copy_input_snapshot,
    build_product_copy_source_fingerprint,
    create_running_product_copy_run,
    enqueue_product_copy,
    is_product_copy_run_stale,
    mark_product_copy_run_failed,
    mark_product_copy_run_succeeded,
)
from app.services.product_copy_review import (
    DuplicateProductCopyReviewError,
    apply_product_copy_review,
    resolve_effective_product_copy,
)
from app.services.product_copy_editorial import create_manual_product_copy_revision, NoCurrentProductCopyError
from app.domain.schemas import ProductCopyManualRevisionRequest
from app.scripts.manual_catalog_snapshot import build_parser as snapshot_parser
from app.scripts.manual_openai_product_copy import build_parser as generation_parser
from app.scripts.manual_product_copy import build_parser as review_parser

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'product-copy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_context(session: Session):
    product = Product(name="Premium Whey", brand=Brand(name="LANDERFIT"))
    session.add(product)
    session.flush()
    skus = [
        SKU(
            id=uuid.UUID(int=20),
            product=product,
            external_sku="CHOC-204-25",
            flavor="Chocolate",
            size_value=Decimal("2"),
            size_unit="LB",
            servings=30,
        ),
        SKU(id=uuid.UUID(int=10), product=product, flavor="Vanilla"),
    ]
    session.add_all(skus)
    session.flush()
    session.add(
        Price(
            sku=skus[0], amount=Decimal("360000"), currency="PYG",
            valid_from=NOW, source="manual", approved=True,
        )
    )
    primary = create_category(session, name="Proteínas", sort_order=10)
    secondary = create_category(session, name="Suplementos", sort_order=20)
    assign_product_category(
        session, product_id=product.id, category_id=primary.id, is_primary=True
    )
    assign_product_category(
        session, product_id=product.id, category_id=secondary.id
    )
    session.flush()
    return product, skus, primary, secondary


def make_payload(session: Session, product: Product, **overrides) -> ProductCopyJobPayload:
    snapshot = build_product_copy_input_snapshot(session, product.id)
    values = {
        "product_id": product.id,
        "copy_type": "short_description",
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "prompt_version": "product-copy-v1",
        "schema_version": PRODUCT_COPY_SCHEMA_VERSION,
        "parameters": {"reasoning_effort": "low"},
        "input_snapshot": snapshot,
        **overrides,
    }
    values["source_fingerprint"] = build_product_copy_source_fingerprint(
        values["input_snapshot"]
    )
    return ProductCopyJobPayload.model_validate(values)


def succeeded_run(
    session: Session,
    product: Product,
    text: str = "Proteína en presentación de 2 LB, disponible en sabor Chocolate.",
) -> ProductCopyRun:
    run = create_running_product_copy_run(
        session, payload=make_payload(session, product), started_at=NOW
    )
    return mark_product_copy_run_succeeded(
        session,
        run,
        structured_result={"short_description": text},
        usage={"input_tokens": 25},
        completed_at=NOW,
    )


def review(
    session: Session,
    run: ProductCopyRun,
    decision: ProductCopyReviewDecision,
    corrected: str | None = None,
) -> ProductCopyReview:
    return apply_product_copy_review(
        session,
        ProductCopyReviewRequest(
            product_copy_run_id=run.id,
            decision=decision,
            corrected_short_description=corrected,
        ),
        applied_at=NOW,
    )


def test_source_snapshot_and_fingerprint_use_only_ordered_canonical_facts(
    session: Session,
) -> None:
    product, skus, primary, secondary = make_context(session)
    first = build_product_copy_input_snapshot(session, product.id)
    second = build_product_copy_input_snapshot(session, product.id)
    serialized = first.model_dump(mode="json", exclude_none=True)

    assert first == second
    assert serialized["brand_name"] == "LANDERFIT"
    assert serialized["product_name"] == "Premium Whey"
    assert serialized["primary_category"] == {
        "category_id": str(primary.id), "name": "Proteínas"
    }
    assert [item["name"] for item in serialized["secondary_categories"]] == [
        "Suplementos"
    ]
    assert [item.sku_id for item in first.variants] == [
        uuid.UUID(int=10), uuid.UUID(int=20)
    ]
    flattened = json.dumps(serialized).casefold()
    assert all(
        forbidden not in flattened
        for forbidden in (
            "price", "amount", "currency", "external_sku", "choc-204-25",
            "branding", "layout", "photo", "image"
        )
    )
    assert build_product_copy_source_fingerprint(first) == (
        build_product_copy_source_fingerprint(second)
    )

    baseline = build_product_copy_source_fingerprint(first)
    product.name = "Premium Whey Plus"
    assert build_product_copy_source_fingerprint(
        build_product_copy_input_snapshot(session, product.id)
    ) != baseline
    product.name = "Premium Whey"
    product.brand.name = "LANDERFIT NUEVA"
    brand_changed = build_product_copy_source_fingerprint(
        build_product_copy_input_snapshot(session, product.id)
    )
    assert brand_changed != baseline
    product.brand.name = "LANDERFIT"
    primary.name = "Proteínas deportivas"
    category_changed = build_product_copy_source_fingerprint(
        build_product_copy_input_snapshot(session, product.id)
    )
    assert category_changed != baseline
    primary.name = "Proteínas"
    skus[0].flavor = "Cookies"
    assert build_product_copy_source_fingerprint(
        build_product_copy_input_snapshot(session, product.id)
    ) != baseline
    assert secondary.id != primary.id


def test_short_description_normalizes_and_rejects_invalid_plain_text() -> None:
    assert ProductCopyResult(
        short_description="  Descripción\n   breve   de prueba.  "
    ).short_description == "Descripción breve de prueba."
    assert len(ProductCopyResult(short_description="x" * 180).short_description) == 180
    for invalid in ("", "   ", "x" * 181, "<b>texto</b>", "**texto**", "- item"):
        with pytest.raises(ValidationError):
            ProductCopyResult(short_description=invalid)
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProductCopyResult.model_validate(
            {"short_description": "Válida.", "long_description": "No permitida"}
        )


def test_enqueue_is_idempotent_but_configuration_and_facts_change_identity(
    session: Session,
) -> None:
    product, *_ = make_context(session)
    first = enqueue_product_copy(session, product_id=product.id)
    same = enqueue_product_copy(session, product_id=product.id)
    assert first.id == same.id
    assert first.idempotency_key == build_product_copy_idempotency_key(
        ProductCopyJobPayload.model_validate(first.payload)
    )

    changed_model = enqueue_product_copy(
        session, product_id=product.id, model="another-model"
    )
    product.name = "Changed canonical Product"
    changed_facts = enqueue_product_copy(session, product_id=product.id)
    assert len({first.id, changed_model.id, changed_facts.id}) == 3


@pytest.mark.parametrize(
    "decision",
    [None, ProductCopyReviewDecision.APPROVED, ProductCopyReviewDecision.CORRECTED,
     ProductCopyReviewDecision.REJECTED],
)
def test_new_editorial_policy_does_not_change_old_proposal_or_review(
    session: Session, decision: ProductCopyReviewDecision | None,
) -> None:
    product, *_ = make_context(session)
    old_run = succeeded_run(session, product, "Resumen antiguo de SKU.")
    old_review = None
    if decision is not None:
        corrected = (
            "Corrección humana."
            if decision is ProductCopyReviewDecision.CORRECTED else None
        )
        old_review = review(session, old_run, decision, corrected)
    session.flush()
    before = (
        old_run.generated_text, old_run.status, old_run.started_at,
        old_run.completed_at, old_run.usage.copy(), old_run.input_snapshot.copy(),
        old_run.prompt_version,
        old_review.decision if old_review else None,
        old_review.applied_at if old_review else None,
        old_review.corrected_short_description if old_review else None,
    )

    new_job = enqueue_product_copy(session, product_id=product.id)

    assert new_job.payload["prompt_version"] == "product-copy-v3"
    assert (
        old_run.generated_text, old_run.status, old_run.started_at,
        old_run.completed_at, old_run.usage, old_run.input_snapshot,
        old_run.prompt_version,
        old_review.decision if old_review else None,
        old_review.applied_at if old_review else None,
        old_review.corrected_short_description if old_review else None,
    ) == before
    resolved = resolve_effective_product_copy(session, product.id)
    if decision is ProductCopyReviewDecision.CORRECTED:
        assert resolved.short_description == "Corrección humana."
    elif decision is ProductCopyReviewDecision.APPROVED:
        assert resolved.short_description == "Resumen antiguo de SKU."
    else:
        assert resolved.state is ProductCopyResolutionState.NONE


def test_run_lifecycle_is_audited_terminal_and_does_not_modify_product(
    session: Session,
) -> None:
    product, *_ = make_context(session)
    original_name = product.name
    payload = make_payload(session, product)
    run = create_running_product_copy_run(session, payload=payload, started_at=NOW)
    stored_snapshot = json.loads(json.dumps(run.input_snapshot))

    mark_product_copy_run_succeeded(
        session,
        run,
        structured_result={"short_description": "  Descripción aprobable.  "},
        usage={"provider_response_id": "resp_1", "total_tokens": 40},
        completed_at=NOW,
    )

    assert product.name == original_name
    assert run.status is ExtractionRunStatus.SUCCEEDED
    assert run.generated_text == "Descripción aprobable."
    assert run.input_snapshot == stored_snapshot
    assert run.source_fingerprint == payload.source_fingerprint
    assert ProductCopyRunRead.model_validate(run).model == "gpt-5.6-sol"
    with pytest.raises(ValueError, match="already complete"):
        mark_product_copy_run_succeeded(
            session, run, structured_result={"short_description": "Otra."}
        )


def test_failed_run_is_sanitized_and_preserves_no_proposal(session: Session) -> None:
    product, *_ = make_context(session)
    run = create_running_product_copy_run(session, payload=make_payload(session, product))
    mark_product_copy_run_failed(
        session, run, error="api_key=sk-sensitive provider response"
    )
    assert run.status is ExtractionRunStatus.FAILED
    assert run.generated_text is None and run.usage is None
    assert "sk-sensitive" not in run.sanitized_error


def test_approve_correct_reject_are_append_only_and_keep_generation_immutable(
    session: Session,
) -> None:
    product, *_ = make_context(session)
    approved_run = succeeded_run(session, product, "Texto generado aprobado.")
    corrected_run = succeeded_run(session, product, "Texto generado original.")
    rejected_run = succeeded_run(session, product, "Texto generado rechazado.")

    approved = review(
        session, approved_run, ProductCopyReviewDecision.APPROVED
    )
    corrected = review(
        session,
        corrected_run,
        ProductCopyReviewDecision.CORRECTED,
        "  Corrección   humana final. ",
    )
    rejected = review(
        session, rejected_run, ProductCopyReviewDecision.REJECTED
    )

    assert approved.applied_at == NOW
    assert corrected.corrected_short_description == "Corrección humana final."
    assert rejected.applied_at is None
    assert corrected_run.generated_text == "Texto generado original."
    assert session.scalars(select(ProductCopyReview)).all() == [
        approved, corrected, rejected
    ]
    with pytest.raises(DuplicateProductCopyReviewError):
        review(session, approved_run, ProductCopyReviewDecision.REJECTED)


def test_resolver_distinguishes_current_stale_and_none_without_timestamp_shortcuts(
    session: Session,
) -> None:
    product, *_ = make_context(session)
    none = resolve_effective_product_copy(session, product.id)
    assert none.state is ProductCopyResolutionState.NONE

    old_run = succeeded_run(session, product, "Texto vigente original.")
    old_review = review(session, old_run, ProductCopyReviewDecision.APPROVED)
    current = resolve_effective_product_copy(session, product.id)
    assert current.state is ProductCopyResolutionState.CURRENT
    assert current.short_description == "Texto vigente original."

    succeeded_run(session, product, "Nueva propuesta no revisada.")
    still_current = resolve_effective_product_copy(session, product.id)
    assert still_current.product_copy_run_id == old_run.id
    assert still_current.short_description == "Texto vigente original."

    product.name = "Product renamed after review"
    session.flush()
    old_review.created_at = NOW + timedelta(days=30)
    session.flush()
    assert is_product_copy_run_stale(session, old_run)
    stale = resolve_effective_product_copy(session, product.id)
    assert stale.state is ProductCopyResolutionState.STALE
    assert stale.short_description == "Texto vigente original."

    new_run = succeeded_run(session, product, "Nueva propuesta sin revisar.")
    assert resolve_effective_product_copy(session, product.id).state is (
        ProductCopyResolutionState.STALE
    )
    review(
        session,
        new_run,
        ProductCopyReviewDecision.CORRECTED,
        "Corrección humana para los hechos actuales.",
    )
    resolved = resolve_effective_product_copy(session, product.id)
    assert resolved.state is ProductCopyResolutionState.CURRENT
    assert resolved.short_description == "Corrección humana para los hechos actuales."


def test_manual_workflow_parsers_are_narrow_and_typed() -> None:
    product_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    assert generation_parser().parse_args(
        ["--product-id", product_id]
    ).product_id == uuid.UUID(product_id)
    assert review_parser().parse_args(
        ["correct", "--run-id", run_id, "--text", "Correccion humana."]
    ).command == "correct"
    assert review_parser().parse_args(
        ["resolve", "--product-id", product_id]
    ).command == "resolve"
    assert snapshot_parser().parse_args(
        ["--product-id", product_id]
    ).product_id == [uuid.UUID(product_id)]


def test_manual_revisions_preserve_ai_history_and_resolve_by_editorial_time(session: Session) -> None:
    product, *_ = make_context(session)
    snapshot = CatalogSnapshot(schema_version="catalog-snapshot-v1", currency="PYG", as_of=NOW, payload={"copy": "Frozen historical copy."}, content_hash="a" * 64)
    session.add(snapshot)
    session.flush()
    snapshot_payload = dict(snapshot.payload)
    with pytest.raises(NoCurrentProductCopyError):
        create_manual_product_copy_revision(session, product_id=product.id, request=ProductCopyManualRevisionRequest(short_description="Early text."))
    run = succeeded_run(session, product, "AI original.")
    ai_review = review(session, run, ProductCopyReviewDecision.APPROVED)
    ai_review.applied_at = NOW
    session.flush()
    original_name = product.name
    first = create_manual_product_copy_revision(session, product_id=product.id, request=ProductCopyManualRevisionRequest(short_description="  Human   wording.  "))
    first.created_at = NOW + timedelta(minutes=1)
    session.flush()
    assert first.short_description == "Human wording."
    assert resolve_effective_product_copy(session, product.id).short_description == "Human wording."
    second = create_manual_product_copy_revision(session, product_id=product.id, request=ProductCopyManualRevisionRequest(short_description="Second wording."))
    second.created_at = NOW + timedelta(minutes=2)
    session.flush()
    resolved = resolve_effective_product_copy(session, product.id)
    assert resolved.product_copy_manual_revision_id == second.id
    assert run.generated_text == "AI original."
    assert ai_review.decision is ProductCopyReviewDecision.APPROVED
    assert first.short_description == "Human wording."
    assert product.name == original_name
    assert snapshot.payload == snapshot_payload
    later_run = succeeded_run(session, product, "Later AI proposal.")
    later_review = review(session, later_run, ProductCopyReviewDecision.APPROVED)
    later_review.applied_at = NOW + timedelta(minutes=3)
    session.flush()
    assert resolve_effective_product_copy(session, product.id).short_description == "Later AI proposal."
    product.name = "Changed canonical facts"
    session.flush()
    assert resolve_effective_product_copy(session, product.id).state is ProductCopyResolutionState.STALE
    assert session.scalars(select(ProductCopyManualRevision)).all() == [first, second]


def test_manual_revision_validation_and_unknown_product(session: Session) -> None:
    for text in ("", " ", "x" * 181):
        with pytest.raises(ValidationError):
            ProductCopyManualRevisionRequest(short_description=text)
    with pytest.raises(ValueError, match="Product not found"):
        create_manual_product_copy_revision(session, product_id=uuid.uuid4(), request=ProductCopyManualRevisionRequest(short_description="Valid."))
