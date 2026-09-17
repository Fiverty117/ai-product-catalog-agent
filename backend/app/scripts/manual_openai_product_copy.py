import argparse
import json
import os
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.ai.openai_product_copy import OpenAIProductCopyProvider
from app.ai.product_copy import configured_openai_product_copy_model
from app.db.models import Job, ProductCopyRun
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.db.types import utc_now
from app.domain.enums import JobStatus
from app.services.jobs import requeue_failed_job
from app.services.product_copy import PRODUCT_COPY_JOB_TYPE, enqueue_product_copy
from app.workers.job_worker import JobWorker
from app.workers.product_copy_handler import ProductCopyJobHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one real OpenAI short Product copy attempt."
    )
    parser.add_argument("--product-id", required=True, type=uuid.UUID)
    parser.add_argument("--model", default=configured_openai_product_copy_model())
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--requeue-failed",
        action="store_true",
        help="Requeue the same failed logical Job when attempt budget remains.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    provider = OpenAIProductCopyProvider.from_environment()
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            job = enqueue_product_copy(
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
                {PRODUCT_COPY_JOB_TYPE: ProductCopyJobHandler(session_factory, provider)},
            )
            if worker.run_once() != job_id:
                raise RuntimeError("target Product copy Job was not eligible")

        with session_factory() as session:
            stored_job = session.get(Job, job_id)
            latest_run = session.scalar(
                select(ProductCopyRun)
                .where(ProductCopyRun.job_id == job_id)
                .order_by(ProductCopyRun.created_at.desc(), ProductCopyRun.id.desc())
                .limit(1)
            )
            if stored_job is None:
                raise RuntimeError("Product copy Job disappeared")
            print(
                json.dumps(
                    {
                        "job_id": str(job_id),
                        "job_status": stored_job.status.value,
                        "job_attempts": stored_job.attempts,
                        "job_error": stored_job.last_error,
                        "run_id": str(latest_run.id) if latest_run else None,
                        "run_status": latest_run.status.value if latest_run else None,
                        "source_fingerprint": (
                            latest_run.source_fingerprint if latest_run else None
                        ),
                        "generated_text": (
                            latest_run.generated_text if latest_run else None
                        ),
                        "usage": latest_run.usage if latest_run else None,
                        "run_error": (
                            latest_run.sanitized_error if latest_run else None
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
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
                "another queued Job is ahead; run the normal worker first"
            )


if __name__ == "__main__":
    main()
