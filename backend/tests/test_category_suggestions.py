import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Brand,
    CategorySuggestionRun,
    Product,
    ProductCategory,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus
from app.domain.schemas import (
    CategorySuggestionJobPayload,
    CategorySuggestionResult,
    CategorySuggestionRunRead,
)
from app.services.categories import create_category, set_category_active
from app.services.category_suggestions import (
    PRODUCT_CATEGORY_SCHEMA_VERSION,
    build_category_suggestion_idempotency_key,
    build_category_suggestion_input_hash,
    build_category_suggestion_input_snapshot,
    create_running_category_suggestion_run,
    enqueue_category_suggestion,
    mark_category_suggestion_run_failed,
    mark_category_suggestion_run_succeeded,
)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'suggestions.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_context(session: Session, *, with_skus: bool = True):
    product = Product(name="Premium Whey", brand=Brand(name="Canonical Brand"))
    session.add(product)
    session.flush()
    if with_skus:
        session.add_all(
            [
                SKU(
                    id=uuid.UUID(int=20),
                    product=product,
                    flavor="Chocolate",
                    size_value=Decimal("2"),
                    size_unit="lb",
                    servings=30,
                ),
                SKU(id=uuid.UUID(int=10), product=product, flavor="Vanilla"),
            ]
        )
    later = create_category(session, name="Zeta", sort_order=20)
    first = create_category(session, name="Proteins", sort_order=10)
    inactive = create_category(session, name="Hidden", sort_order=1)
    set_category_active(session, category_id=inactive.id, is_active=False)
    session.flush()
    return product, first, later, inactive


def make_payload(session: Session, product: Product, **overrides):
    snapshot = build_category_suggestion_input_snapshot(session, product.id)
    values = {
        "product_id": product.id,
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "prompt_version": "product-category-v1",
        "schema_version": PRODUCT_CATEGORY_SCHEMA_VERSION,
        "parameters": {"reasoning_effort": "low"},
        "input_snapshot": snapshot,
        **overrides,
    }
    values["input_hash"] = build_category_suggestion_input_hash(
        input_snapshot=values["input_snapshot"],
        provider=values["provider"],
        model=values["model"],
        prompt_version=values["prompt_version"],
        schema_version=values["schema_version"],
        parameters=values["parameters"],
    )
    return CategorySuggestionJobPayload.model_validate(values)


def result(primary_id, secondary_ids=()):
    return CategorySuggestionResult.model_validate(
        {
            "primary": (
                {
                    "category_id": primary_id,
                    "confidence": "0.9",
                    "evidence": "Product name identifies its category.",
                }
                if primary_id is not None
                else None
            ),
            "secondary": [
                {
                    "category_id": category_id,
                    "confidence": "0.7",
                    "evidence": "Canonical context supports a secondary category.",
                }
                for category_id in secondary_ids
            ],
        }
    )


def test_snapshot_uses_only_canonical_ordered_context(session: Session) -> None:
    product, first, later, inactive = make_context(session)

    snapshot = build_category_suggestion_input_snapshot(session, product.id)
    serialized = snapshot.model_dump(mode="json", exclude_none=True)

    assert serialized["product"] == {
        "product_id": str(product.id),
        "product_name": "Premium Whey",
    }
    assert serialized["brand"]["brand_name"] == "Canonical Brand"
    assert [item.sku_id for item in snapshot.sku_variants] == [
        uuid.UUID(int=10),
        uuid.UUID(int=20),
    ]
    assert [item.category_id for item in snapshot.taxonomy] == [first.id, later.id]
    assert inactive.id not in {item.category_id for item in snapshot.taxonomy}
    flattened = json.dumps(serialized).casefold()
    assert all(
        forbidden not in flattened
        for forbidden in ("price", "file_path", "image", "api_key", "secret")
    )


def test_snapshot_supports_product_without_skus(session: Session) -> None:
    product, *_ = make_context(session, with_skus=False)

    assert build_category_suggestion_input_snapshot(session, product.id).sku_variants == []


def test_hash_and_enqueue_idempotency_track_relevant_state(session: Session) -> None:
    product, *_ = make_context(session)
    first = enqueue_category_suggestion(session, product_id=product.id)
    same = enqueue_category_suggestion(session, product_id=product.id)
    baseline_key = first.idempotency_key

    assert same is first
    product.name = "Renamed Product"
    renamed = enqueue_category_suggestion(session, product_id=product.id)
    assert renamed.idempotency_key != baseline_key

    create_category(session, name="New Taxonomy Entry")
    taxonomy_changed = enqueue_category_suggestion(session, product_id=product.id)
    assert taxonomy_changed.idempotency_key != renamed.idempotency_key

    product.skus[0].flavor = "Changed SKU context"
    sku_changed = enqueue_category_suggestion(session, product_id=product.id)
    assert sku_changed.idempotency_key != taxonomy_changed.idempotency_key


def test_taxonomy_rename_and_deactivation_change_logical_request(
    session: Session,
) -> None:
    product, first_category, later_category, _ = make_context(session)
    baseline = enqueue_category_suggestion(session, product_id=product.id)

    first_category.name = "Protein Products"
    renamed = enqueue_category_suggestion(session, product_id=product.id)
    assert renamed.idempotency_key != baseline.idempotency_key

    set_category_active(
        session, category_id=later_category.id, is_active=False
    )
    deactivated = enqueue_category_suggestion(session, product_id=product.id)
    assert deactivated.idempotency_key != renamed.idempotency_key


@pytest.mark.parametrize(
    "override",
    [
        {"model": "other-model"},
        {"prompt_version": "product-category-v2"},
        {"schema_version": "product-category-result-v2"},
        {"parameters": {"reasoning_effort": "low", "max_output_tokens": 300}},
    ],
)
def test_config_change_changes_logical_hash(session: Session, override: dict) -> None:
    product, *_ = make_context(session)
    baseline = make_payload(session, product)
    changed = make_payload(session, product, **override)

    assert changed.input_hash != baseline.input_hash
    assert build_category_suggestion_idempotency_key(changed) != (
        build_category_suggestion_idempotency_key(baseline)
    )


def test_structured_output_contract_and_numeric_decimal_schema() -> None:
    category_id = uuid.uuid4()
    valid = result(category_id)
    empty = result(None)
    schema_text = json.dumps(CategorySuggestionResult.model_json_schema())

    assert isinstance(valid.primary.confidence, Decimal)
    assert empty.primary is None and empty.secondary == []
    assert "pattern" not in schema_text
    assert '"type": "number"' in schema_text
    with pytest.raises(ValidationError, match="must be unique"):
        result(None, [category_id, category_id])
    with pytest.raises(ValidationError, match="also be secondary"):
        result(category_id, [category_id])
    with pytest.raises(ValidationError):
        CategorySuggestionResult.model_validate(
            {
                "primary": {
                    "category_id": category_id,
                    "confidence": "1.1",
                    "evidence": "Invalid confidence.",
                },
                "secondary": [],
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        CategorySuggestionResult.model_validate(
            {"primary": None, "secondary": [], "category_name": "Forbidden"}
        )


def test_unknown_result_id_rejected_against_snapshot(session: Session) -> None:
    product, first, *_ = make_context(session)
    snapshot = build_category_suggestion_input_snapshot(session, product.id)

    with pytest.raises(ValidationError, match="not in the input taxonomy"):
        CategorySuggestionResult.model_validate(
            result(uuid.uuid4()),
            context={"taxonomy_ids": {item.category_id for item in snapshot.taxonomy}},
        )
    assert CategorySuggestionResult.model_validate(
        result(first.id),
        context={"taxonomy_ids": {item.category_id for item in snapshot.taxonomy}},
    ).primary.category_id == first.id


def test_run_lifecycle_persists_snapshot_and_validated_result(session: Session) -> None:
    product, first, *_ = make_context(session)
    payload = make_payload(session, product)
    started = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    run = create_running_category_suggestion_run(
        session, payload=payload, started_at=started
    )
    historical_snapshot = json.loads(json.dumps(run.input_snapshot))

    mark_category_suggestion_run_succeeded(
        session,
        run,
        structured_result=result(first.id),
        usage={"input_tokens": 100},
    )
    first.name = "Renamed Later"
    session.flush()

    assert run.status is ExtractionRunStatus.SUCCEEDED
    assert run.input_snapshot == historical_snapshot
    assert run.structured_result["primary"]["category_id"] == str(first.id)
    assert run.usage == {"input_tokens": 100}
    run_read = CategorySuggestionRunRead.model_validate(run)
    assert run_read.input_hash == payload.input_hash
    assert run_read.parameters == payload.parameters
    assert session.scalar(select(ProductCategory)) is None


def test_failed_run_is_sanitized_and_terminal(session: Session) -> None:
    product, *_ = make_context(session)
    run = create_running_category_suggestion_run(
        session, payload=make_payload(session, product)
    )

    mark_category_suggestion_run_failed(
        session, run, error="api_key=sk-sensitive provider body"
    )

    assert run.status is ExtractionRunStatus.FAILED
    assert "sk-sensitive" not in run.sanitized_error
    assert run.structured_result is None
    with pytest.raises(ValueError, match="already complete"):
        mark_category_suggestion_run_succeeded(
            session, run, structured_result=result(None)
        )
