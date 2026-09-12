from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job
from app.domain.enums import JobStatus


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
