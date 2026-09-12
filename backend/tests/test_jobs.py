from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, Job
from app.db.session import create_sqlite_engine
from app.domain.enums import JobStatus
from app.services.jobs import enqueue_job
from app.workers.job_worker import JobWorker


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def job_store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield engine, session_factory
    engine.dispose()


def persist_job(session_factory, **overrides) -> Job:
    parameters = {
        "job_type": "test.echo",
        "payload": {"photo_id": "photo-1"},
        "idempotency_key": "test.echo:photo-1:v1",
        "max_attempts": 3,
        **overrides,
    }
    with session_factory() as session:
        job = enqueue_job(session, **parameters)
        session.commit()
        return job


def load_job(session_factory, job_id) -> Job:
    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_enqueue_creates_queued_job_without_committing(job_store) -> None:
    _, session_factory = job_store
    with session_factory() as session:
        job = enqueue_job(
            session,
            job_type=" extraction.photo ",
            payload={"photo_id": "abc"},
            idempotency_key=" photo:abc:extract:v1 ",
            max_attempts=4,
        )

        assert job.status is JobStatus.QUEUED
        assert job.job_type == "extraction.photo"
        assert job.idempotency_key == "photo:abc:extract:v1"
        assert job.attempts == 0
        assert job.max_attempts == 4
        assert job.created_at.utcoffset() == timedelta(0)

        with session_factory() as other_session:
            assert other_session.get(Job, job.id) is None


def test_duplicate_idempotency_key_reuses_existing_job(job_store) -> None:
    _, session_factory = job_store
    with session_factory() as session:
        first = enqueue_job(
            session,
            job_type="test.first",
            payload={"value": 1},
            idempotency_key="same-operation",
            max_attempts=2,
        )
        second = enqueue_job(
            session,
            job_type="test.different",
            payload={"value": 2},
            idempotency_key="same-operation",
            max_attempts=5,
        )

        assert second is first
        assert second.job_type == "test.first"
        assert second.payload == {"value": 1}
        assert len(session.scalars(select(Job)).all()) == 1


def test_successful_handler_execution(job_store) -> None:
    _, session_factory = job_store
    job = persist_job(session_factory)
    handled_payloads = []
    clock = FixedClock(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))
    worker = JobWorker(
        session_factory,
        {"test.echo": handled_payloads.append},
        clock=clock,
    )

    assert worker.run_once() == job.id

    stored = load_job(session_factory, job.id)
    assert handled_payloads == [{"photo_id": "photo-1"}]
    assert stored.status is JobStatus.SUCCEEDED
    assert stored.attempts == 1
    assert stored.started_at == clock.current
    assert stored.finished_at == clock.current
    assert stored.last_error is None


def test_handler_failure_requeues_then_succeeds(job_store) -> None:
    _, session_factory = job_store
    job = persist_job(session_factory)
    clock = FixedClock(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))
    calls = 0

    def fail_once(payload) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary provider failure")

    worker = JobWorker(session_factory, {"test.echo": fail_once}, clock=clock)
    worker.run_once()

    retriable = load_job(session_factory, job.id)
    assert retriable.status is JobStatus.QUEUED
    assert retriable.attempts == 1
    assert retriable.next_retry_at == clock.current + timedelta(seconds=5)
    assert retriable.last_error == "RuntimeError: temporary provider failure"

    assert worker.run_once() is None
    clock.current = retriable.next_retry_at
    worker.run_once()

    succeeded = load_job(session_factory, job.id)
    assert succeeded.status is JobStatus.SUCCEEDED
    assert succeeded.attempts == 2
    assert calls == 2


def test_permanent_failure_after_max_attempts(job_store) -> None:
    _, session_factory = job_store
    job = persist_job(session_factory, max_attempts=2)
    clock = FixedClock(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))

    def always_fail(payload) -> None:
        raise ValueError("invalid result")

    worker = JobWorker(session_factory, {"test.echo": always_fail}, clock=clock)
    worker.run_once()
    first_failure = load_job(session_factory, job.id)
    assert first_failure.next_retry_at is not None

    clock.current = first_failure.next_retry_at
    worker.run_once()

    failed = load_job(session_factory, job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.attempts == 2
    assert failed.next_retry_at is None
    assert failed.finished_at == clock.current


def test_future_retry_job_is_not_claimed_early(job_store) -> None:
    _, session_factory = job_store
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    job = persist_job(session_factory)
    with session_factory() as session:
        stored = session.get(Job, job.id)
        stored.next_retry_at = now + timedelta(minutes=1)
        session.commit()

    worker = JobWorker(session_factory, {"test.echo": lambda payload: None}, clock=lambda: now)

    assert worker.run_once() is None
    assert load_job(session_factory, job.id).attempts == 0


def test_claim_transaction_is_closed_before_handler_runs(job_store) -> None:
    engine, _ = job_store
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
    job = persist_job(tracking_factory)
    created_sessions.clear()

    def assert_claim_session_closed(payload) -> None:
        assert len(created_sessions) == 1
        assert created_sessions[0].was_closed is True
        with Session(engine) as observer:
            running = observer.get(Job, job.id)
            assert running is not None
            assert running.status is JobStatus.RUNNING

    worker = JobWorker(tracking_factory, {"test.echo": assert_claim_session_closed})
    worker.run_once()

    assert load_job(tracking_factory, job.id).status is JobStatus.SUCCEEDED


def test_stale_running_job_recovery(job_store) -> None:
    _, session_factory = job_store
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    stale = persist_job(session_factory, idempotency_key="stale")
    fresh = persist_job(session_factory, idempotency_key="fresh")
    with session_factory() as session:
        stale_record = session.get(Job, stale.id)
        stale_record.status = JobStatus.RUNNING
        stale_record.attempts = 1
        stale_record.started_at = now - timedelta(minutes=11)
        fresh_record = session.get(Job, fresh.id)
        fresh_record.status = JobStatus.RUNNING
        fresh_record.attempts = 1
        fresh_record.started_at = now - timedelta(minutes=9)
        session.commit()

    worker = JobWorker(session_factory, {}, clock=lambda: now)

    assert worker.recover_stale_jobs(timedelta(minutes=10)) == 1
    recovered = load_job(session_factory, stale.id)
    untouched = load_job(session_factory, fresh.id)
    assert recovered.status is JobStatus.QUEUED
    assert recovered.next_retry_at == now
    assert recovered.started_at is None
    assert recovered.attempts == 1
    assert untouched.status is JobStatus.RUNNING


def test_unknown_job_type_fails_safely(job_store) -> None:
    _, session_factory = job_store
    job = persist_job(
        session_factory,
        job_type="unknown.operation",
        idempotency_key="unknown",
        max_attempts=1,
    )

    JobWorker(session_factory, {}).run_once()

    failed = load_job(session_factory, job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.attempts == 1
    assert failed.last_error == (
        "UnknownJobTypeError: no handler registered for job type: unknown.operation"
    )
