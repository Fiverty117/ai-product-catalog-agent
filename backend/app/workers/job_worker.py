import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Job
from app.db.types import utc_now
from app.domain.enums import JobStatus
from app.services.jobs import PermanentJobError

JobHandler = Callable[["ClaimedJob"], None]
SessionFactory = Callable[[], Session]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    job_type: str
    payload: dict[str, Any]


class UnknownJobTypeError(RuntimeError):
    pass


class JobWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        handlers: Mapping[str, JobHandler],
        *,
        clock: Clock = utc_now,
        retry_base: timedelta = timedelta(seconds=5),
        retry_cap: timedelta = timedelta(minutes=5),
    ) -> None:
        if retry_base <= timedelta(0):
            raise ValueError("retry_base must be positive")
        if retry_cap < retry_base:
            raise ValueError("retry_cap must be at least retry_base")
        self._session_factory = session_factory
        self._handlers = dict(handlers)
        self._clock = clock
        self._retry_base = retry_base
        self._retry_cap = retry_cap

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        if not job_type.strip():
            raise ValueError("job_type is required")
        self._handlers[job_type.strip()] = handler

    def run_once(self) -> uuid.UUID | None:
        claimed = self._claim_one()
        if claimed is None:
            return None

        try:
            handler = self._handlers.get(claimed.job_type)
            if handler is None:
                raise UnknownJobTypeError(
                    f"no handler registered for job type: {claimed.job_type}"
                )
            handler(claimed)
        except Exception as exc:
            self._record_failure(claimed.id, exc)
        else:
            self._record_success(claimed.id)
        return claimed.id

    def recover_stale_jobs(self, stale_after: timedelta) -> int:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")

        now = self._clock()
        stale_before = now - stale_after
        with self._session_factory() as session:
            stale_jobs = session.scalars(
                select(Job).where(
                    Job.status == JobStatus.RUNNING,
                    Job.started_at.is_not(None),
                    Job.started_at <= stale_before,
                )
            ).all()
            for job in stale_jobs:
                job.last_error = "recovered after stale running timeout"
                if job.attempts >= job.max_attempts:
                    job.status = JobStatus.FAILED
                    job.finished_at = now
                    job.next_retry_at = None
                else:
                    job.status = JobStatus.QUEUED
                    job.started_at = None
                    job.finished_at = None
                    job.next_retry_at = now
            session.commit()
            return len(stale_jobs)

    def _claim_one(self) -> ClaimedJob | None:
        now = self._clock()
        with self._session_factory() as session:
            job = session.scalar(
                select(Job)
                .where(
                    Job.status == JobStatus.QUEUED,
                    or_(Job.next_retry_at.is_(None), Job.next_retry_at <= now),
                )
                .order_by(Job.created_at, Job.id)
                .limit(1)
            )
            if job is None:
                return None

            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.started_at = now
            job.finished_at = None
            job.next_retry_at = None
            claimed = ClaimedJob(
                id=job.id,
                job_type=job.job_type,
                payload=job.payload,
            )
            session.commit()
            return claimed

    def _record_success(self, job_id: uuid.UUID) -> None:
        now = self._clock()
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = JobStatus.SUCCEEDED
            job.finished_at = now
            job.next_retry_at = None
            job.last_error = None
            session.commit()

    def _record_failure(self, job_id: uuid.UUID, error: Exception) -> None:
        now = self._clock()
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return

            job.last_error = _concise_error(error)
            if isinstance(error, PermanentJobError) or job.attempts >= job.max_attempts:
                job.status = JobStatus.FAILED
                job.finished_at = now
                job.next_retry_at = None
            else:
                job.status = JobStatus.QUEUED
                job.started_at = None
                job.finished_at = None
                job.next_retry_at = now + self._retry_delay(job.attempts)
            session.commit()

    def _retry_delay(self, attempts: int) -> timedelta:
        delay = self._retry_base
        remaining_doublings = max(attempts - 1, 0)
        while remaining_doublings and delay < self._retry_cap:
            delay = min(delay * 2, self._retry_cap)
            remaining_doublings -= 1
        return delay


def _concise_error(error: Exception, limit: int = 1000) -> str:
    message = " ".join(str(error).split())
    summary = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return summary[:limit]
