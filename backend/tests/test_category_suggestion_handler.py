from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.category_suggestion import (
    CategorySuggestionResponse,
    RetryableCategorySuggestionProviderError,
)
from app.db import Base, Brand, CategorySuggestionRun, Job, Product, ProductCategory
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus, JobStatus
from app.domain.schemas import CategorySuggestionResult
from app.services.categories import create_category
from app.services.category_suggestions import (
    PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE,
    enqueue_category_suggestion,
)
from app.workers.category_suggestion_handler import (
    ProductCategorySuggestionJobHandler,
)
from app.workers.job_worker import JobWorker


class FakeProvider:
    name = "openai"

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def suggest(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(category_id):
    return CategorySuggestionResponse(
        structured_result=CategorySuggestionResult.model_validate(
            {
                "primary": {
                    "category_id": category_id,
                    "confidence": "1",
                    "evidence": "Canonical Product name supports this category.",
                },
                "secondary": [],
            }
        ),
        usage={"provider_response_id": "resp_fake", "input_tokens": 20},
    )


def setup_store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'handler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        product = Product(name="Whey", brand=Brand(name="Brand"))
        session.add(product)
        category = create_category(session, name="Proteins")
        job = enqueue_category_suggestion(session, product_id=product.id)
        session.commit()
        return engine, factory, product.id, category.id, job.id


def test_handler_completes_run_without_canonical_mutation(tmp_path) -> None:
    engine, factory, product_id, category_id, job_id = setup_store(tmp_path)
    provider = FakeProvider(response(category_id))

    JobWorker(
        factory,
        {
            PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE:
            ProductCategorySuggestionJobHandler(factory, provider)
        },
    ).run_once()

    with factory() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(CategorySuggestionRun))
        assert job.status is JobStatus.SUCCEEDED
        assert run.status is ExtractionRunStatus.SUCCEEDED
        assert run.product_id == product_id
        assert run.usage["provider_response_id"] == "resp_fake"
        assert session.scalar(select(ProductCategory)) is None
    assert len(provider.requests) == 1
    engine.dispose()


def test_retry_creates_distinct_immutable_runs(tmp_path) -> None:
    engine, factory, _, category_id, job_id = setup_store(tmp_path)
    provider = FakeProvider(
        RetryableCategorySuggestionProviderError("temporary failure"),
        response(category_id),
    )
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    worker = JobWorker(
        factory,
        {
            PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE:
            ProductCategorySuggestionJobHandler(factory, provider)
        },
        clock=lambda: now,
    )

    worker.run_once()
    with factory() as session:
        failed = session.scalar(select(CategorySuggestionRun))
        retry_at = session.get(Job, job_id).next_retry_at
        failed_snapshot = failed.input_snapshot
        assert failed.status is ExtractionRunStatus.FAILED
    now = retry_at
    worker.run_once()

    with factory() as session:
        runs = session.scalars(
            select(CategorySuggestionRun).order_by(CategorySuggestionRun.created_at)
        ).all()
        assert len(runs) == 2
        assert runs[0].id != runs[1].id
        assert runs[0].status is ExtractionRunStatus.FAILED
        assert runs[1].status is ExtractionRunStatus.SUCCEEDED
        assert runs[0].input_snapshot == failed_snapshot == runs[1].input_snapshot
    engine.dispose()


def test_provider_call_has_no_open_database_session(tmp_path) -> None:
    engine, _, _, category_id, job_id = setup_store(tmp_path)
    sessions = []

    class TrackingSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.was_closed = False
            sessions.append(self)

        def close(self):
            self.was_closed = True
            super().close()

    factory = sessionmaker(
        bind=engine, class_=TrackingSession, expire_on_commit=False
    )

    class InspectingProvider(FakeProvider):
        def suggest(self, request):
            assert sessions and all(item.was_closed for item in sessions)
            with Session(engine) as observer:
                assert observer.get(Job, job_id).status is JobStatus.RUNNING
                assert observer.scalar(select(CategorySuggestionRun)).status is (
                    ExtractionRunStatus.RUNNING
                )
            return super().suggest(request)

    provider = InspectingProvider(response(category_id))
    JobWorker(
        factory,
        {
            PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE:
            ProductCategorySuggestionJobHandler(factory, provider)
        },
    ).run_once()
    engine.dispose()
