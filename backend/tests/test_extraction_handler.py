from datetime import datetime, timezone
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.vision import (
    PermanentVisionProviderError,
    RetryableVisionProviderError,
    VisionExtractionResponse,
)
from app.db import Base, Brand, ExtractionRun, Job, Product, SKU
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus, JobStatus
from app.domain.schemas import ProductExtractionJobPayload, ProductExtractionResult
from app.services.extraction import (
    PRODUCT_EXTRACTION_JOB_TYPE,
    build_product_extraction_idempotency_key,
)
from app.services.jobs import enqueue_job
from app.services.photo_intake import register_original_photo
from app.workers.extraction_handler import ProductExtractionJobHandler
from app.workers.job_worker import JobWorker


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class FakeProvider:
    name = "openai"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests = []

    def extract(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def job_store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'handler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield engine, factory, tmp_path / "originals"
    engine.dispose()


def image_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


def extraction_result() -> ProductExtractionResult:
    return ProductExtractionResult.model_validate(
        {
            "brand_name": observation("Observed Brand"),
            "product_name": observation("Observed Product"),
            "flavor": observation("Vanilla"),
            "size_value": observation("750.000000"),
            "size_unit": observation("g"),
            "servings": observation(None, state="not_legible"),
        }
    )


def observation(value, *, state: str = "extracted") -> dict:
    return {
        "value": value,
        "confidence": "0.85" if value is not None else None,
        "evidence": None,
        "state": state,
    }


def success_response() -> VisionExtractionResponse:
    return VisionExtractionResponse(
        structured_result=extraction_result(),
        usage={
            "provider_response_id": "resp_fake",
            "input_tokens": 100,
            "cached_input_tokens": 10,
            "output_tokens": 40,
            "reasoning_tokens": 12,
            "total_tokens": 140,
        },
    )


def create_sku(session: Session) -> SKU:
    sku = SKU(product=Product(name="Canonical Product", brand=Brand(name="Canonical Brand")))
    session.add(sku)
    session.flush()
    return sku


def enqueue_extraction(session_factory, originals_dir, *, colors=((10, 20, 30),), max_attempts=3):
    with session_factory() as session:
        sku = create_sku(session)
        photos = [
            register_original_photo(
                session,
                image_bytes=image_bytes(color),
                original_filename=f"photo-{index}.png",
                originals_dir=originals_dir,
                sku_id=sku.id,
            )
            for index, color in enumerate(colors)
        ]
        payload = ProductExtractionJobPayload(
            photo_ids=[photo.id for photo in reversed(photos)],
            provider="openai",
            model="gpt-5.6-sol",
            prompt_version="product-extraction-v1",
            schema_version="product-result-v1",
            parameters={"reasoning_effort": "low", "image_detail": "high"},
        )
        key = build_product_extraction_idempotency_key(
            payload,
            {photo.id: photo.checksum_sha256 for photo in photos},
        )
        job = enqueue_job(
            session,
            job_type=PRODUCT_EXTRACTION_JOB_TYPE,
            payload=payload.model_dump(mode="json"),
            idempotency_key=key,
            max_attempts=max_attempts,
        )
        session.commit()
        return job.id, sku.id, photos


def load_runs(session_factory, job_id) -> list[ExtractionRun]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(ExtractionRun)
                .where(ExtractionRun.job_id == job_id)
                .order_by(ExtractionRun.created_at, ExtractionRun.id)
            ).all()
        )


def test_handler_succeeds_in_canonical_photo_order_without_canonical_mutation(job_store) -> None:
    _, session_factory, originals_dir = job_store
    job_id, sku_id, photos = enqueue_extraction(
        session_factory,
        originals_dir,
        colors=((10, 20, 30), (40, 50, 60)),
    )
    provider = FakeProvider(success_response())
    worker = JobWorker(
        session_factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(session_factory, provider)},
    )

    assert worker.run_once() == job_id

    with session_factory() as session:
        job = session.get(Job, job_id)
        sku = session.get(SKU, sku_id)
        runs = session.scalars(select(ExtractionRun)).all()
        assert job is not None and job.status is JobStatus.SUCCEEDED
        assert sku is not None
        assert sku.flavor is None
        assert sku.size_value is None
        assert sku.field_provenance == []
        assert len(runs) == 1
        assert runs[0].status is ExtractionRunStatus.SUCCEEDED
        assert runs[0].sku_id == sku_id
        assert runs[0].usage == success_response().usage
        assert runs[0].structured_result["flavor"]["value"] == "Vanilla"
    expected_checksums = sorted(photo.checksum_sha256 for photo in photos)
    assert [image.checksum_sha256 for image in provider.requests[0].images] == expected_checksums


def test_retry_creates_a_distinct_extraction_run(job_store) -> None:
    _, session_factory, originals_dir = job_store
    job_id, _, _ = enqueue_extraction(session_factory, originals_dir)
    provider = FakeProvider(
        RetryableVisionProviderError("temporary OpenAI service failure"),
        success_response(),
    )
    clock = FixedClock(datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc))
    worker = JobWorker(
        session_factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(session_factory, provider)},
        clock=clock,
    )

    worker.run_once()
    first_runs = load_runs(session_factory, job_id)
    with session_factory() as session:
        retry_at = session.get(Job, job_id).next_retry_at
    assert len(first_runs) == 1
    assert first_runs[0].status is ExtractionRunStatus.FAILED
    assert retry_at is not None

    clock.current = retry_at
    worker.run_once()

    runs = load_runs(session_factory, job_id)
    assert len(runs) == 2
    assert runs[0].id != runs[1].id
    assert [run.status for run in runs] == [
        ExtractionRunStatus.FAILED,
        ExtractionRunStatus.SUCCEEDED,
    ]
    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job.status is JobStatus.SUCCEEDED
        assert job.attempts == 2


def test_permanent_provider_failure_stops_generic_job_retry(job_store) -> None:
    _, session_factory, originals_dir = job_store
    job_id, _, _ = enqueue_extraction(session_factory, originals_dir, max_attempts=3)
    provider = FakeProvider(
        PermanentVisionProviderError("OpenAI request or access was rejected")
    )
    worker = JobWorker(
        session_factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(session_factory, provider)},
    )

    worker.run_once()

    with session_factory() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(ExtractionRun).where(ExtractionRun.job_id == job_id))
        assert job.status is JobStatus.FAILED
        assert job.attempts == 1
        assert job.next_retry_at is None
        assert job.last_error == (
            "PermanentVisionProviderError: OpenAI request or access was rejected"
        )
        assert run.status is ExtractionRunStatus.FAILED


def test_invalid_structured_provider_result_is_retried(job_store) -> None:
    _, session_factory, originals_dir = job_store
    job_id, _, _ = enqueue_extraction(session_factory, originals_dir)
    invalid = VisionExtractionResponse(
        structured_result={"unexpected": True},  # type: ignore[arg-type]
        usage={},
    )
    provider = FakeProvider(invalid)
    worker = JobWorker(
        session_factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(session_factory, provider)},
    )

    worker.run_once()

    with session_factory() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(ExtractionRun).where(ExtractionRun.job_id == job_id))
        assert job.status is JobStatus.QUEUED
        assert job.next_retry_at is not None
        assert run.status is ExtractionRunStatus.FAILED
        assert "failed validation" in run.sanitized_error


def test_corrupt_photo_is_permanent_and_provider_is_not_called(job_store) -> None:
    _, session_factory, originals_dir = job_store
    job_id, _, photos = enqueue_extraction(session_factory, originals_dir)
    with open(photos[0].file_path, "ab") as stored_photo:
        stored_photo.write(b"corrupt")
    provider = FakeProvider(success_response())
    worker = JobWorker(
        session_factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(session_factory, provider)},
    )

    worker.run_once()

    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job.status is JobStatus.FAILED
        assert job.attempts == 1
        assert session.scalar(select(ExtractionRun)) is None
    assert provider.requests == []


def test_malformed_job_payload_fails_permanently_without_provider_call(job_store) -> None:
    _, session_factory, _ = job_store
    with session_factory() as session:
        job = enqueue_job(
            session,
            job_type=PRODUCT_EXTRACTION_JOB_TYPE,
            payload={"photo_ids": []},
            idempotency_key="product.extract.v1:malformed",
            max_attempts=3,
        )
        session.commit()
        job_id = job.id
    provider = FakeProvider(success_response())
    worker = JobWorker(
        session_factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(session_factory, provider)},
    )

    worker.run_once()

    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job.status is JobStatus.FAILED
        assert job.attempts == 1
        assert session.scalar(select(ExtractionRun)) is None
    assert provider.requests == []


def test_provider_call_has_no_open_database_session(job_store) -> None:
    engine, _, originals_dir = job_store
    created_sessions = []

    class TrackingSession(Session):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.was_closed = False
            created_sessions.append(self)

        def close(self) -> None:
            self.was_closed = True
            super().close()

    tracking_factory = sessionmaker(
        bind=engine,
        class_=TrackingSession,
        expire_on_commit=False,
    )
    job_id, _, _ = enqueue_extraction(tracking_factory, originals_dir)
    created_sessions.clear()

    class InspectingProvider(FakeProvider):
        def extract(self, request):
            assert len(created_sessions) == 2
            assert all(session.was_closed for session in created_sessions)
            with Session(engine) as observer:
                job = observer.get(Job, job_id)
                run = observer.scalar(
                    select(ExtractionRun).where(ExtractionRun.job_id == job_id)
                )
                assert job.status is JobStatus.RUNNING
                assert run.status is ExtractionRunStatus.RUNNING
            return super().extract(request)

    provider = InspectingProvider(success_response())
    JobWorker(
        tracking_factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(tracking_factory, provider)},
    ).run_once()

    with tracking_factory() as session:
        assert session.get(Job, job_id).status is JobStatus.SUCCEEDED
