from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.product_copy import (
    ProductCopyResponse,
    RetryableProductCopyProviderError,
)
from app.ai.prompts.product_copy_v2 import PRODUCT_COPY_PROMPT as PRODUCT_COPY_PROMPT_V2
from app.ai.prompts.product_copy_v3 import PRODUCT_COPY_PROMPT as PRODUCT_COPY_PROMPT_V3
from app.db import Base, Brand, Job, Product, ProductCopyRun
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus, JobStatus
from app.domain.schemas import ProductCopyResult
from app.services.product_copy import PRODUCT_COPY_JOB_TYPE, enqueue_product_copy
from app.workers.job_worker import JobWorker
from app.workers.product_copy_handler import ProductCopyJobHandler


class FakeProvider:
    name = "openai"

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(text="Descripcion factual para el producto de prueba."):
    return ProductCopyResponse(
        structured_result=ProductCopyResult(short_description=text),
        usage={"provider_response_id": "resp_fake", "input_tokens": 20},
    )


def setup_store(tmp_path, *, prompt_version=None):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'copy-handler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        product = Product(name="Whey", brand=Brand(name="Brand"))
        session.add(product)
        session.flush()
        values = {"product_id": product.id}
        if prompt_version is not None:
            values["prompt_version"] = prompt_version
        job = enqueue_product_copy(session, **values)
        session.commit()
        return engine, factory, product.id, job.id


def test_handler_completes_audited_run_without_product_mutation(tmp_path) -> None:
    engine, factory, product_id, job_id = setup_store(tmp_path)
    provider = FakeProvider(response())

    JobWorker(
        factory,
        {PRODUCT_COPY_JOB_TYPE: ProductCopyJobHandler(factory, provider)},
    ).run_once()

    with factory() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(ProductCopyRun))
        product = session.get(Product, product_id)
        assert job.status is JobStatus.SUCCEEDED
        assert run.status is ExtractionRunStatus.SUCCEEDED
        assert run.product_id == product_id
        assert run.generated_text == "Descripcion factual para el producto de prueba."
        assert run.input_snapshot == job.payload["input_snapshot"]
        assert run.prompt_version == "product-copy-v3"
        assert run.usage["provider_response_id"] == "resp_fake"
        assert product.name == "Whey"
    assert len(provider.requests) == 1
    assert provider.requests[0].prompt_version == "product-copy-v3"
    assert "canonical PRODUCT" in provider.requests[0].prompt
    engine.dispose()


def test_handler_preserves_v2_prompt_for_already_queued_job(tmp_path) -> None:
    engine, factory, _, _ = setup_store(
        tmp_path, prompt_version="product-copy-v2"
    )
    provider = FakeProvider(response())

    JobWorker(
        factory,
        {PRODUCT_COPY_JOB_TYPE: ProductCopyJobHandler(factory, provider)},
    ).run_once()

    assert provider.requests[0].prompt_version == "product-copy-v2"
    assert provider.requests[0].prompt == PRODUCT_COPY_PROMPT_V2
    assert provider.requests[0].prompt != PRODUCT_COPY_PROMPT_V3
    engine.dispose()


def test_retry_creates_distinct_immutable_attempt_runs(tmp_path) -> None:
    engine, factory, _, job_id = setup_store(tmp_path)
    provider = FakeProvider(
        RetryableProductCopyProviderError("temporary failure"),
        response("Nueva propuesta valida."),
    )
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    worker = JobWorker(
        factory,
        {PRODUCT_COPY_JOB_TYPE: ProductCopyJobHandler(factory, provider)},
        clock=lambda: now,
    )

    worker.run_once()
    with factory() as session:
        first = session.scalar(select(ProductCopyRun))
        retry_at = session.get(Job, job_id).next_retry_at
        first_snapshot = first.input_snapshot.copy()
        assert first.status is ExtractionRunStatus.FAILED
        assert first.generated_text is None
    now = retry_at
    worker.run_once()

    with factory() as session:
        runs = session.scalars(
            select(ProductCopyRun).order_by(ProductCopyRun.created_at)
        ).all()
        assert len(runs) == 2
        assert runs[0].id != runs[1].id
        assert runs[0].status is ExtractionRunStatus.FAILED
        assert runs[1].status is ExtractionRunStatus.SUCCEEDED
        assert runs[0].input_snapshot == first_snapshot == runs[1].input_snapshot
    engine.dispose()


def test_provider_call_has_no_open_database_session(tmp_path) -> None:
    engine, _, _, job_id = setup_store(tmp_path)
    sessions = []

    class TrackingSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.was_closed = False
            sessions.append(self)

        def close(self):
            self.was_closed = True
            super().close()

    factory = sessionmaker(bind=engine, class_=TrackingSession, expire_on_commit=False)

    class InspectingProvider(FakeProvider):
        def generate(self, request):
            assert sessions and all(item.was_closed for item in sessions)
            with Session(engine) as observer:
                assert observer.get(Job, job_id).status is JobStatus.RUNNING
                assert observer.scalar(select(ProductCopyRun)).status is (
                    ExtractionRunStatus.RUNNING
                )
            return super().generate(request)

    provider = InspectingProvider(response())
    JobWorker(
        factory,
        {PRODUCT_COPY_JOB_TYPE: ProductCopyJobHandler(factory, provider)},
    ).run_once()
    engine.dispose()
