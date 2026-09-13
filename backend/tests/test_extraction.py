import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import Base, Brand, ExtractionRun, Job, Photo, Product, SKU
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus, ObservationState, PhotoRole
from app.domain.schemas import (
    ExtractionRunRead,
    FieldObservation,
    ProductExtractionJobPayload,
    ProductExtractionResult,
)
from app.services.extraction import (
    PRODUCT_EXTRACTION_JOB_TYPE,
    CompletedExtractionRunError,
    build_product_extraction_idempotency_key,
    create_running_extraction_run,
    mark_extraction_run_failed,
    mark_extraction_run_succeeded,
)
from app.services.jobs import enqueue_job


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'extraction.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_sku(session: Session) -> SKU:
    sku = SKU(product=Product(name="Whey", brand=Brand(name="Test Brand")))
    session.add(sku)
    session.flush()
    return sku


def make_photo(session: Session, checksum_character: str, sku: SKU | None = None) -> Photo:
    photo = Photo(
        sku=sku,
        file_path=f"storage/originals/{checksum_character}.jpg",
        checksum_sha256=checksum_character * 64,
        original_filename=f"{checksum_character}.jpg",
        mime_type="image/jpeg",
        file_size_bytes=100,
        width=10,
        height=20,
        role=PhotoRole.FRONT,
        is_original=True,
    )
    session.add(photo)
    session.flush()
    return photo


def make_payload(photo_ids: list[uuid.UUID], **overrides) -> ProductExtractionJobPayload:
    values = {
        "photo_ids": photo_ids,
        "provider": "test-provider",
        "model": "vision-model-1",
        "prompt_version": "product-extraction-v1",
        "schema_version": "product-result-v1",
        "parameters": {"temperature": 0, "detail": "high"},
        **overrides,
    }
    return ProductExtractionJobPayload.model_validate(values)


def make_result() -> ProductExtractionResult:
    return ProductExtractionResult.model_validate(
        {
            "brand_name": {
                "value": "Optimum Nutrition",
                "confidence": "0.96",
                "evidence": "Brand visible on front label",
                "state": "extracted",
            },
            "product_name": {
                "value": "Gold Standard Whey",
                "confidence": "0.94",
                "evidence": None,
                "state": "extracted",
            },
            "flavor": {
                "value": "Vanilla",
                "confidence": "0.81",
                "evidence": None,
                "state": "extracted",
            },
            "size_value": {
                "value": 2.5,
                "confidence": "0.88",
                "evidence": "Net weight panel",
                "state": "extracted",
            },
            "size_unit": {
                "value": "lb",
                "confidence": "0.88",
                "evidence": "Net weight panel",
                "state": "extracted",
            },
            "servings": {
                "value": None,
                "confidence": None,
                "evidence": "Nutrition panel obscured",
                "state": "not_legible",
            },
        }
    )


def test_valid_extraction_result_schema_uses_decimal_size() -> None:
    result = make_result()

    assert result.brand_name.state is ObservationState.EXTRACTED
    assert result.size_value.value == Decimal("2.500000")
    assert isinstance(result.size_value.value, Decimal)
    assert result.servings.state is ObservationState.NOT_LEGIBLE


def test_extraction_result_json_schema_uses_numeric_decimal_contract() -> None:
    schema = ProductExtractionResult.model_json_schema()
    serialized_schema = json.dumps(schema)
    size_reference = schema["properties"]["size_value"]["$ref"]
    size_observation = schema["$defs"][size_reference.rsplit("/", 1)[1]]
    size_value_options = size_observation["properties"]["value"]["anyOf"]

    assert "pattern" not in serialized_schema
    assert "(?" not in serialized_schema
    assert {"type": "number", "exclusiveMinimum": 0} in size_value_options
    assert {"type": "null"} in size_value_options
    assert all(option.get("type") != "string" for option in size_value_options)


@pytest.mark.parametrize(
    "invalid_size",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("1.0000001"),
        Decimal("1234567890123.123456"),
    ],
)
def test_extraction_result_still_rejects_invalid_decimal_sizes(
    invalid_size: Decimal,
) -> None:
    data = make_result().model_dump()
    data["size_value"]["value"] = invalid_size

    with pytest.raises(ValidationError):
        ProductExtractionResult.model_validate(data)


@pytest.mark.parametrize("confidence", [Decimal("-0.000001"), Decimal("1.000001")])
def test_observation_rejects_invalid_confidence(confidence: Decimal) -> None:
    with pytest.raises(ValidationError):
        FieldObservation[str](
            value="Vanilla",
            confidence=confidence,
            state=ObservationState.EXTRACTED,
        )


def test_extracted_observation_requires_value() -> None:
    with pytest.raises(ValidationError, match="require a value"):
        FieldObservation[str](value=None, state=ObservationState.EXTRACTED)


@pytest.mark.parametrize(
    "state", [ObservationState.NOT_LEGIBLE, ObservationState.NOT_PRESENT]
)
def test_non_value_observation_states_require_null(state: ObservationState) -> None:
    with pytest.raises(ValidationError, match="require value=null"):
        FieldObservation[str](value="invented", state=state)


def test_extraction_result_rejects_unknown_fields() -> None:
    result = make_result().model_dump(mode="json")
    result["category"] = {
        "value": "supplements",
        "confidence": "1",
        "evidence": None,
        "state": "extracted",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProductExtractionResult.model_validate(result)


def test_job_payload_requires_at_least_one_unique_photo() -> None:
    with pytest.raises(ValidationError):
        make_payload([])

    photo_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="must be unique"):
        make_payload([photo_id, photo_id])


def test_job_payload_rejects_secrets_and_paths_in_parameters() -> None:
    with pytest.raises(ValidationError, match="not allowed"):
        make_payload(
            [uuid.uuid4()],
            parameters={"fallbacks": [{"api_key": "must-not-be-stored"}]},
        )


def test_idempotency_key_is_canonical_for_ordering() -> None:
    first_photo_id = uuid.uuid4()
    second_photo_id = uuid.uuid4()
    first = make_payload(
        [first_photo_id, second_photo_id],
        parameters={"detail": "high", "sampling": {"seed": 7, "temperature": 0}},
    )
    reordered = make_payload(
        [second_photo_id, first_photo_id],
        parameters={"sampling": {"temperature": 0, "seed": 7}, "detail": "high"},
    )
    checksums = {first_photo_id: "a" * 64, second_photo_id: "b" * 64}

    first_key = build_product_extraction_idempotency_key(first, checksums)
    reordered_key = build_product_extraction_idempotency_key(reordered, checksums)

    assert first_key == reordered_key
    assert first_key.startswith(f"{PRODUCT_EXTRACTION_JOB_TYPE}:")
    assert len(first_key.removeprefix(f"{PRODUCT_EXTRACTION_JOB_TYPE}:")) == 64


def test_idempotency_changes_for_every_relevant_input() -> None:
    photo_id = uuid.uuid4()
    payload = make_payload([photo_id])
    checksums = {photo_id: "a" * 64}
    baseline = build_product_extraction_idempotency_key(payload, checksums)

    variants = [
        payload.model_copy(update={"provider": "other-provider"}),
        payload.model_copy(update={"model": "vision-model-2"}),
        payload.model_copy(update={"prompt_version": "product-extraction-v2"}),
        payload.model_copy(update={"schema_version": "product-result-v2"}),
        payload.model_copy(update={"parameters": {"temperature": 0.1}}),
    ]
    for variant in variants:
        assert build_product_extraction_idempotency_key(variant, checksums) != baseline
    assert (
        build_product_extraction_idempotency_key(payload, {photo_id: "b" * 64})
        != baseline
    )


def test_extraction_run_with_one_unassigned_photo(session: Session) -> None:
    photo = make_photo(session, "a")
    started_at = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    run = create_running_extraction_run(
        session,
        payload=make_payload([photo.id]),
        started_at=started_at,
    )

    assert run.status is ExtractionRunStatus.RUNNING
    assert run.sku_id is None
    assert run.job_id is None
    assert run.started_at == started_at
    assert run.photos == [photo]
    assert run in photo.extraction_runs


def test_extraction_run_supports_multiple_photos(session: Session) -> None:
    front = make_photo(session, "a")
    back = make_photo(session, "b")

    run = create_running_extraction_run(
        session,
        payload=make_payload([front.id, back.id]),
    )
    session.commit()
    session.expire_all()

    stored = session.get(ExtractionRun, run.id)
    assert stored is not None
    assert {photo.id for photo in stored.photos} == {front.id, back.id}


def test_extraction_run_may_reference_existing_sku_and_job(session: Session) -> None:
    sku = make_sku(session)
    photo = make_photo(session, "c", sku=sku)
    payload = make_payload([photo.id])
    job = enqueue_job(
        session,
        job_type=PRODUCT_EXTRACTION_JOB_TYPE,
        payload=payload.model_dump(mode="json"),
        idempotency_key="product.extract.v1:test",
    )

    run = create_running_extraction_run(
        session,
        payload=payload,
        job_id=job.id,
        sku_id=sku.id,
    )

    assert run.sku is sku
    assert run.job is job
    assert run in sku.extraction_runs
    assert run in job.extraction_runs


def test_successful_completion_persists_validated_observation_only(session: Session) -> None:
    sku = make_sku(session)
    photo = make_photo(session, "d", sku=sku)
    run = create_running_extraction_run(
        session,
        payload=make_payload([photo.id]),
        sku_id=sku.id,
    )
    completed_at = datetime(2026, 9, 12, 12, 5, tzinfo=timezone.utc)

    mark_extraction_run_succeeded(
        session,
        run,
        structured_result=make_result(),
        usage={"input_tokens": 120, "estimated_cost": "0.0012"},
        completed_at=completed_at,
    )
    session.commit()

    read_model = ExtractionRunRead.model_validate(run)
    assert run.status is ExtractionRunStatus.SUCCEEDED
    assert run.completed_at == completed_at
    assert read_model.structured_result is not None
    assert read_model.structured_result.size_value.value == Decimal("2.500000")
    assert run.usage == {"input_tokens": 120, "estimated_cost": "0.0012"}
    assert sku.flavor is None
    assert sku.field_provenance == []


def test_failed_run_records_only_sanitized_error(session: Session) -> None:
    photo = make_photo(session, "e")
    run = create_running_extraction_run(
        session,
        payload=make_payload([photo.id]),
    )

    mark_extraction_run_failed(
        session,
        run,
        error=RuntimeError(
            "provider rejected api_key=secret-value authorization=Bearer token-123 "
            "credential sk-rawtoken"
        ),
    )

    assert run.status is ExtractionRunStatus.FAILED
    assert run.completed_at is not None
    assert run.structured_result is None
    assert "secret-value" not in run.sanitized_error
    assert "token-123" not in run.sanitized_error
    assert "sk-rawtoken" not in run.sanitized_error
    assert "[REDACTED]" in run.sanitized_error


def test_completed_run_cannot_be_overwritten(session: Session) -> None:
    photo = make_photo(session, "f")
    run = create_running_extraction_run(
        session,
        payload=make_payload([photo.id]),
    )
    result = make_result()
    mark_extraction_run_succeeded(session, run, structured_result=result)
    stored_result = run.structured_result.copy()

    with pytest.raises(CompletedExtractionRunError):
        mark_extraction_run_failed(session, run, error="late failure")
    with pytest.raises(CompletedExtractionRunError):
        mark_extraction_run_succeeded(session, run, structured_result=result)

    assert run.status is ExtractionRunStatus.SUCCEEDED
    assert run.structured_result == stored_result
