import argparse
import json
import os
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.ai.category_suggestion import configured_openai_category_model
from app.ai.openai_category_suggestion import OpenAICategorySuggestionProvider
from app.db.models import CategorySuggestionRun, Job
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.db.types import utc_now
from app.domain.enums import JobStatus
from app.services.category_suggestions import (
    PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE,
    enqueue_category_suggestion,
)
from app.services.jobs import requeue_failed_job
from app.workers.category_suggestion_handler import (
    ProductCategorySuggestionJobHandler,
)
from app.workers.job_worker import JobWorker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one real OpenAI Product category suggestion attempt."
    )
    parser.add_argument("--product-id", required=True, type=uuid.UUID)
    parser.add_argument("--model", default=configured_openai_category_model())
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--requeue-failed",
        action="store_true",
        help="Requeue the same failed logical Job when attempt budget remains.",
    )
    args = parser.parse_args()

    provider = OpenAICategorySuggestionProvider.from_environment()
    database_url = os.environ.get("DATABASE_URL", DATABASE_URL)
    engine = create_sqlite_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            job = enqueue_category_suggestion(
                session,
                product_id=args.product_id,
                model=args.model,
                parameters={"reasoning_effort": "low"},
                max_attempts=args.max_attempts,
            )
            if job.status is JobStatus.FAILED and args.requeue_failed:
                requeue_failed_job(session, job)
            session.commit()
            job_id = job.id
            status = job.status

        if status is JobStatus.QUEUED:
            _require_job_is_next_eligible(session_factory, job_id)
            worker = JobWorker(
                session_factory,
                {
                    PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE:
                    ProductCategorySuggestionJobHandler(session_factory, provider)
                },
            )
            if worker.run_once() != job_id:
                raise RuntimeError("target category suggestion Job was not eligible")

        with session_factory() as session:
            stored_job = session.get(Job, job_id)
            latest_run = session.scalar(
                select(CategorySuggestionRun)
                .where(CategorySuggestionRun.job_id == job_id)
                .order_by(
                    CategorySuggestionRun.created_at.desc(),
                    CategorySuggestionRun.id.desc(),
                )
                .limit(1)
            )
            if stored_job is None:
                raise RuntimeError("category suggestion Job disappeared")
            summary = {
                "job_id": str(job_id),
                "job_status": stored_job.status.value,
                "job_attempts": stored_job.attempts,
                "job_error": stored_job.last_error,
                "run_id": str(latest_run.id) if latest_run else None,
                "run_status": latest_run.status.value if latest_run else None,
                "input_hash": latest_run.input_hash if latest_run else None,
                "structured_result": latest_run.structured_result if latest_run else None,
                "usage": latest_run.usage if latest_run else None,
                "run_error": latest_run.sanitized_error if latest_run else None,
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            if stored_job.status is not JobStatus.SUCCEEDED:
                raise SystemExit(1)
    finally:
        engine.dispose()


def _require_job_is_next_eligible(session_factory, job_id: uuid.UUID) -> None:
    now = utc_now()
    with session_factory() as session:
        next_job = session.scalar(
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED,
                or_(Job.next_retry_at.is_(None), Job.next_retry_at <= now),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        if next_job is None or next_job.id != job_id:
            raise RuntimeError(
                "another queued Job is ahead of this smoke-test Job; run the normal "
                "worker or clear that queue item before retrying"
            )


if __name__ == "__main__":
    main()
