import argparse
import json
import os
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.ai.image_enhancement import configured_openai_image_model
from app.ai.openai_image_enhancement import OpenAIImageEnhancementProvider
from app.db.models import DerivedImage, ImageEnhancementRun, Job
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.db.types import utc_now
from app.domain.enums import JobStatus
from app.services.image_enhancement import (
    IMAGE_ENHANCEMENT_JOB_TYPE,
    enqueue_image_enhancement,
)
from app.services.jobs import requeue_failed_job
from app.workers.image_enhancement_handler import ImageEnhancementJobHandler
from app.workers.job_worker import JobWorker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one real OpenAI image enhancement worker attempt."
    )
    parser.add_argument("--photo-id", required=True, type=uuid.UUID)
    parser.add_argument("--model", default=configured_openai_image_model())
    parser.add_argument("--quality", default="high")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--requeue-failed",
        action="store_true",
        help="Requeue the same failed logical Job if its attempt budget remains.",
    )
    args = parser.parse_args()

    provider = OpenAIImageEnhancementProvider.from_environment()
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            job = enqueue_image_enhancement(
                session,
                source_photo_id=args.photo_id,
                model=args.model,
                parameters={"quality": args.quality},
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
                    IMAGE_ENHANCEMENT_JOB_TYPE: ImageEnhancementJobHandler(
                        session_factory,
                        provider,
                    )
                },
            )
            if worker.run_once() != job_id:
                raise RuntimeError("target image enhancement Job was not eligible")

        with session_factory() as session:
            job = session.get(Job, job_id)
            run = session.scalar(
                select(ImageEnhancementRun)
                .where(ImageEnhancementRun.job_id == job_id)
                .order_by(
                    ImageEnhancementRun.created_at.desc(),
                    ImageEnhancementRun.id.desc(),
                )
                .limit(1)
            )
            derived = (
                session.scalar(
                    select(DerivedImage).where(
                        DerivedImage.enhancement_run_id == run.id
                    )
                )
                if run is not None
                else None
            )
            if job is None:
                raise RuntimeError("image enhancement Job disappeared")
            print(
                json.dumps(
                    {
                        "job_id": str(job.id),
                        "job_status": job.status.value,
                        "job_attempts": job.attempts,
                        "job_error": job.last_error,
                        "run_id": str(run.id) if run else None,
                        "run_status": run.status.value if run else None,
                        "run_error": run.sanitized_error if run else None,
                        "derived_image_id": str(derived.id) if derived else None,
                        "file_path": derived.file_path if derived else None,
                        "checksum_sha256": (
                            derived.checksum_sha256 if derived else None
                        ),
                        "width": derived.width if derived else None,
                        "height": derived.height if derived else None,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            if job.status is not JobStatus.SUCCEEDED:
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
