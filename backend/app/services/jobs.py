from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job
from app.db.types import utc_now
from app.domain.enums import JobStatus


class PermanentJobError(RuntimeError):
    """A deterministic failure that should bypass remaining retry attempts."""


class JobRecoveryError(ValueError):
    """A failed job cannot be explicitly requeued within its attempt budget."""


def enqueue_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_attempts: int = 3,
) -> Job:
    """Create one queued job, or reuse the job with the same idempotency key."""

    normalized_job_type = job_type.strip()
    normalized_key = idempotency_key.strip()
    if not normalized_job_type:
        raise ValueError("job_type is required")
    if not normalized_key:
        raise ValueError("idempotency_key is required")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    existing = session.scalar(
        select(Job).where(Job.idempotency_key == normalized_key)
    )
    if existing is not None:
        return existing

    job = Job(
        job_type=normalized_job_type,
        status=JobStatus.QUEUED,
        payload=payload,
        idempotency_key=normalized_key,
        attempts=0,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    return job


def requeue_failed_job(
    session: Session,
    job: Job,
    *,
    retry_at: datetime | None = None,
) -> Job:
    """Explicitly requeue one failed job without resetting its attempt history."""

    if job.status is not JobStatus.FAILED:
        raise JobRecoveryError("only a failed job may be requeued")
    if job.attempts >= job.max_attempts:
        raise JobRecoveryError("the failed job has exhausted its attempt budget")

    job.status = JobStatus.QUEUED
    job.next_retry_at = retry_at or utc_now()
    job.started_at = None
    job.finished_at = None
    session.flush()
    return job
