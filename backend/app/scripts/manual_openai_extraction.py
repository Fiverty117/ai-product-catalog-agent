import argparse
import json
import os
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.ai.openai_vision import (
    OPENAI_PROVIDER,
    OpenAIVisionProvider,
    configured_openai_vision_model,
)
from app.ai.prompts.product_extraction_v1 import PROMPT_VERSION
from app.db.models import ExtractionRun, Job, Photo
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.db.types import utc_now
from app.domain.enums import JobStatus
from app.domain.schemas import ProductExtractionJobPayload
from app.services.extraction import (
    PRODUCT_EXTRACTION_JOB_TYPE,
    PRODUCT_EXTRACTION_SCHEMA_VERSION,
    build_product_extraction_idempotency_key,
)
from app.services.jobs import enqueue_job, requeue_failed_job
from app.workers.extraction_handler import ProductExtractionJobHandler
from app.workers.job_worker import JobWorker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one real OpenAI product extraction worker attempt."
    )
    parser.add_argument(
        "--photo-id",
        action="append",
        required=True,
        type=uuid.UUID,
        dest="photo_ids",
        help="Registered original Photo UUID; repeat for multiple photos.",
    )
    parser.add_argument("--model", default=configured_openai_vision_model())
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--requeue-failed",
        action="store_true",
        help=(
            "Requeue the existing failed idempotent job when it still has an "
            "unused attempt. Attempts are not reset."
        ),
    )
    args = parser.parse_args()

    provider = OpenAIVisionProvider.from_environment()
    database_url = os.environ.get("DATABASE_URL", DATABASE_URL)
    engine = create_sqlite_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        payload = ProductExtractionJobPayload(
            photo_ids=args.photo_ids,
            provider=OPENAI_PROVIDER,
            model=args.model,
            prompt_version=PROMPT_VERSION,
            schema_version=PRODUCT_EXTRACTION_SCHEMA_VERSION,
            parameters={"reasoning_effort": "low", "image_detail": "high"},
        )
        with session_factory() as session:
            photos = session.scalars(
                select(Photo).where(Photo.id.in_(payload.photo_ids))
            ).all()
            if {photo.id for photo in photos} != set(payload.photo_ids):
                raise RuntimeError("one or more requested Photo records do not exist")
            checksums = {photo.id: photo.checksum_sha256 for photo in photos}
            key = build_product_extraction_idempotency_key(payload, checksums)
            job = enqueue_job(
                session,
                job_type=PRODUCT_EXTRACTION_JOB_TYPE,
                payload=payload.model_dump(mode="json"),
                idempotency_key=key,
                max_attempts=args.max_attempts,
            )
            if job.status is JobStatus.FAILED and args.requeue_failed:
                requeue_failed_job(session, job)
            session.commit()
            job_id = job.id
            job_status = job.status

        if job_status is JobStatus.QUEUED:
            _require_job_is_next_eligible(session_factory, job_id)
            worker = JobWorker(
                session_factory,
                {
                    PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(
                        session_factory,
                        provider,
                    )
                },
            )
            if worker.run_once() != job_id:
                raise RuntimeError("target extraction job was not eligible")

        with session_factory() as session:
            stored_job = session.get(Job, job_id)
            latest_run = session.scalar(
                select(ExtractionRun)
                .where(ExtractionRun.job_id == job_id)
                .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
                .limit(1)
            )
            if stored_job is None:
                raise RuntimeError("extraction job disappeared during execution")
            summary = {
                "job_id": str(job_id),
                "job_status": stored_job.status.value,
                "job_attempts": stored_job.attempts,
                "job_error": stored_job.last_error,
                "run_id": str(latest_run.id) if latest_run else None,
                "run_status": latest_run.status.value if latest_run else None,
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
                "another queued job is ahead of this smoke-test job; run the normal "
                "worker or clear that queue item before retrying"
            )


if __name__ == "__main__":
    main()
