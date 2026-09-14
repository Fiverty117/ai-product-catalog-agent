import hashlib
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import app.workers.image_enhancement_handler as enhancement_handler_module
from app.ai.image_enhancement import (
    ImageEnhancementResult,
    PermanentImageEnhancementProviderError,
    RetryableImageEnhancementProviderError,
)
from app.db import (
    Base,
    Brand,
    DerivedImage,
    ImageEnhancementRun,
    Job,
    Photo,
    Price,
    Product,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus, JobStatus, PhotoRole
from app.services.catalog_readiness import evaluate_product_catalog_readiness
from app.services.categories import assign_product_category, create_category
from app.services.image_enhancement import (
    DerivedImageStorageError,
    IMAGE_ENHANCEMENT_JOB_TYPE,
    enqueue_image_enhancement,
)
from app.services.photo_intake import register_original_photo
from app.workers.image_enhancement_handler import ImageEnhancementJobHandler
from app.workers.job_worker import JobWorker


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.current


class FakeProvider:
    name = "openai"

    def __init__(self, *outcomes, observer=None) -> None:
        self.outcomes = list(outcomes)
        self.requests = []
        self.observer = observer

    def enhance(self, request):
        self.requests.append(request)
        if self.observer:
            self.observer()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'handler.db'}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    yield factory, tmp_path / "originals", tmp_path / "processed"
    engine.dispose()


def image_bytes(image_format="PNG", color=(10, 20, 30)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (11, 9), color).save(output, format=image_format)
    return output.getvalue()


def enqueue(factory, originals, *, max_attempts=3):
    with factory() as session:
        sku = SKU(product=Product(name="Product", brand=Brand(name="Brand")))
        session.add(sku)
        session.flush()
        photo = register_original_photo(
            session,
            image_bytes=image_bytes(),
            original_filename="front.png",
            originals_dir=originals,
            sku_id=sku.id,
            role=PhotoRole.FRONT,
        )
        job = enqueue_image_enhancement(
            session,
            source_photo_id=photo.id,
            max_attempts=max_attempts,
        )
        session.commit()
        return job.id, photo.id


def success_result() -> ImageEnhancementResult:
    return ImageEnhancementResult(
        output_bytes=image_bytes(color=(80, 90, 100)),
        output_format_hint="png",
        usage={"input_tokens": 10, "output_tokens": 20},
    )


def worker(factory, processed, provider, clock=None):
    return JobWorker(
        factory,
        {
            IMAGE_ENHANCEMENT_JOB_TYPE: ImageEnhancementJobHandler(
                factory, provider, processed_dir=processed
            )
        },
        **({"clock": clock} if clock else {}),
    )


def runs(factory, job_id):
    with factory() as session:
        return list(
            session.scalars(
                select(ImageEnhancementRun)
                .where(ImageEnhancementRun.job_id == job_id)
                .order_by(ImageEnhancementRun.created_at, ImageEnhancementRun.id)
            ).all()
        )


def test_success_commits_running_before_provider_and_creates_derived_image(store):
    factory, originals, processed = store
    job_id, photo_id = enqueue(factory, originals)

    def observe():
        with factory() as session:
            run = session.scalar(select(ImageEnhancementRun))
            assert run is not None and run.status is ExtractionRunStatus.RUNNING
            assert session.get(Job, job_id).status is JobStatus.RUNNING
            assert session.scalar(select(DerivedImage)) is None

    provider = FakeProvider(success_result(), observer=observe)
    assert worker(factory, processed, provider).run_once() == job_id

    with factory() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(ImageEnhancementRun))
        derived = session.scalar(select(DerivedImage))
        photo = session.get(Photo, photo_id)
        assert job.status is JobStatus.SUCCEEDED
        assert run.status is ExtractionRunStatus.SUCCEEDED
        assert derived.source_photo_id == photo.id == run.source_photo_id
        assert derived.enhancement_run_id == run.id
        assert Path(derived.file_path).is_relative_to(processed.resolve())
        assert photo.file_path != derived.file_path
        assert photo.sku_id is not None and photo.product_id is None
        assert run.usage == success_result().usage
    assert provider.requests[0].source.content
    assert "content=" not in repr(provider.requests[0].source)


def test_retry_creates_distinct_run_without_overwriting_failure(store):
    factory, originals, processed = store
    job_id, _ = enqueue(factory, originals)
    provider = FakeProvider(
        RetryableImageEnhancementProviderError("temporary"), success_result()
    )
    clock = FixedClock()
    active_worker = worker(factory, processed, provider, clock)

    active_worker.run_once()
    first = runs(factory, job_id)
    with factory() as session:
        retry_at = session.get(Job, job_id).next_retry_at
    clock.current = retry_at
    active_worker.run_once()
    attempts = runs(factory, job_id)

    assert len(first) == 1
    assert len(attempts) == 2
    assert attempts[0].id != attempts[1].id
    assert [run.status for run in attempts] == [
        ExtractionRunStatus.FAILED,
        ExtractionRunStatus.SUCCEEDED,
    ]


def test_provider_failure_is_sanitized_and_creates_no_derived_image(store):
    factory, originals, processed = store
    job_id, _ = enqueue(factory, originals)
    provider = FakeProvider(
        PermanentImageEnhancementProviderError("api_key=sk-sensitive rejected")
    )

    worker(factory, processed, provider).run_once()

    with factory() as session:
        run = session.scalar(select(ImageEnhancementRun))
        assert session.get(Job, job_id).status is JobStatus.FAILED
        assert run.status is ExtractionRunStatus.FAILED
        assert "sk-sensitive" not in run.sanitized_error
        assert session.scalar(select(DerivedImage)) is None


@pytest.mark.parametrize("failure", ["missing", "checksum", "corrupt", "unsupported"])
def test_invalid_source_assets_fail_permanently_before_provider(store, failure):
    factory, originals, processed = store
    job_id, photo_id = enqueue(factory, originals)
    with factory() as session:
        photo = session.get(Photo, photo_id)
        path = Path(photo.file_path)
        if failure == "missing":
            path.unlink()
        elif failure == "checksum":
            path.write_bytes(image_bytes(color=(1, 2, 3)))
        else:
            content = b"corrupt" if failure == "corrupt" else image_bytes("GIF")
            path.write_bytes(content)
            checksum = hashlib.sha256(content).hexdigest()
            photo.checksum_sha256 = checksum
            job = session.get(Job, job_id)
            job.payload = {**job.payload, "source_checksum_sha256": checksum}
            session.commit()
    provider = FakeProvider(success_result())

    worker(factory, processed, provider).run_once()

    with factory() as session:
        assert session.get(Job, job_id).status is JobStatus.FAILED
        assert session.scalar(select(ImageEnhancementRun)).status is ExtractionRunStatus.FAILED
        assert session.scalar(select(DerivedImage)) is None
    assert provider.requests == []


def test_corrupt_provider_output_never_succeeds(store):
    factory, originals, processed = store
    first_job, _ = enqueue(factory, originals)
    corrupt = ImageEnhancementResult(
        output_bytes=b"not-image", output_format_hint="png", usage={}
    )
    worker(factory, processed, FakeProvider(corrupt)).run_once()
    with factory() as session:
        first_run = session.scalar(
            select(ImageEnhancementRun).where(ImageEnhancementRun.job_id == first_job)
        )
        assert first_run.status is ExtractionRunStatus.FAILED
        assert session.scalar(select(DerivedImage)) is None


def test_storage_failure_marks_attempt_failed_and_job_permanent(store, monkeypatch):
    factory, originals, processed = store
    job_id, _ = enqueue(factory, originals)

    def fail_storage(*args, **kwargs):
        raise DerivedImageStorageError("storage/C:/sensitive/path failed")

    monkeypatch.setattr(
        enhancement_handler_module, "store_processed_image", fail_storage
    )
    worker(factory, processed, FakeProvider(success_result())).run_once()

    with factory() as session:
        run = session.scalar(
            select(ImageEnhancementRun).where(ImageEnhancementRun.job_id == job_id)
        )
        assert session.get(Job, job_id).status is JobStatus.FAILED
        assert run.status is ExtractionRunStatus.FAILED
        assert "sensitive" not in run.sanitized_error
        assert session.scalar(select(DerivedImage)) is None


def test_database_completion_failure_keeps_audit_and_retries_job(store, monkeypatch):
    factory, originals, processed = store
    job_id, _ = enqueue(factory, originals)

    def fail_completion(*args, **kwargs):
        raise RuntimeError("database completion failed")

    monkeypatch.setattr(
        enhancement_handler_module,
        "complete_image_enhancement_run",
        fail_completion,
    )
    worker(factory, processed, FakeProvider(success_result())).run_once()

    with factory() as session:
        run = session.scalar(
            select(ImageEnhancementRun).where(ImageEnhancementRun.job_id == job_id)
        )
        job = session.get(Job, job_id)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 1
        assert run.status is ExtractionRunStatus.FAILED
        assert session.scalar(select(DerivedImage)) is None


def test_derived_output_does_not_change_catalog_readiness_hero(store):
    factory, originals, processed = store
    with factory() as session:
        product = Product(name="Ready", brand=Brand(name="Ready Brand"))
        sku = SKU(product=product)
        session.add(sku)
        session.flush()
        category = create_category(session, name="Ready Category")
        assign_product_category(
            session, product_id=product.id, category_id=category.id, is_primary=True
        )
        price = Price(
            sku=sku,
            amount="10.0000",
            currency="USD",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source="manual",
            approved=True,
        )
        session.add(price)
        source = register_original_photo(
            session,
            image_bytes=image_bytes(),
            original_filename="front.png",
            originals_dir=originals,
            sku_id=sku.id,
            role=PhotoRole.FRONT,
        )
        job = enqueue_image_enhancement(session, source_photo_id=source.id)
        session.commit()
        job_id, product_id, source_id = job.id, product.id, source.id

    before = None
    with factory() as session:
        before = evaluate_product_catalog_readiness(
            session,
            product_id=product_id,
            currency="USD",
            as_of=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
    worker(factory, processed, FakeProvider(success_result())).run_once()
    with factory() as session:
        after = evaluate_product_catalog_readiness(
            session,
            product_id=product_id,
            currency="USD",
            as_of=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        assert session.get(Job, job_id).status is JobStatus.SUCCEEDED
        assert session.scalar(select(DerivedImage)) is not None

    assert before.hero_photo_id == after.hero_photo_id == source_id
    assert before.is_ready is after.is_ready is True
