import argparse
import json
import os
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.db.models import CatalogArtifact, CatalogRenderRun, Job
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.db.types import utc_now
from app.domain.enums import JobStatus
from app.domain.schemas import CatalogRenderConfig
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.services.catalog_rendering import (
    enqueue_catalog_render_v2,
)
from app.services.catalog_branding import get_active_catalog_brand_profile
from app.services.jobs import requeue_failed_job
from app.workers.catalog_render_handler import catalog_render_handlers
from app.workers.job_worker import JobWorker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one local Chromium catalog PDF render attempt."
    )
    parser.add_argument("--snapshot-id", required=True, type=uuid.UUID)
    parser.add_argument("--brand-key", required=True, help="Explicit active catalog publisher profile key.")
    parser.add_argument("--locale", default="es-PY")
    parser.add_argument(
        "--layout",
        choices=("classic", "dense", "compact"),
        default="classic",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--requeue-failed",
        action="store_true",
        help="Requeue the same failed logical Job when attempt budget remains.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            profile = get_active_catalog_brand_profile(session, key=args.brand_key)
            job = enqueue_catalog_render_v2(
                session,
                catalog_snapshot_id=args.snapshot_id,
                brand_profile_id=profile.id,
                config=CatalogRenderConfig(locale=args.locale, layout=args.layout),
                max_attempts=args.max_attempts,
            )
            if job.status is JobStatus.FAILED and args.requeue_failed:
                requeue_failed_job(session, job)
            session.commit()
            job_id = job.id
            status = job.status
            profile_id = profile.id
            profile_key = profile.key
            branding_hash = job.payload["branding_hash"]
            layout_key = job.payload["layout_key"]
            layout_version = job.payload["layout_version"]

        if status is JobStatus.QUEUED:
            _require_job_is_next_eligible(session_factory, job_id)
            worker = JobWorker(
                session_factory,
                catalog_render_handlers(session_factory, ChromiumCatalogPdfRenderer()),
            )
            if worker.run_once() != job_id:
                raise RuntimeError("target catalog render Job was not eligible")

        with session_factory() as session:
            stored_job = session.get(Job, job_id)
            run = session.scalar(
                select(CatalogRenderRun)
                .where(CatalogRenderRun.job_id == job_id)
                .order_by(
                    CatalogRenderRun.created_at.desc(),
                    CatalogRenderRun.id.desc(),
                )
                .limit(1)
            )
            artifact = (
                session.scalar(
                    select(CatalogArtifact).where(
                        CatalogArtifact.render_run_id == run.id
                    )
                )
                if run is not None
                else None
            )
            if stored_job is None:
                raise RuntimeError("catalog render Job disappeared")
            print(
                json.dumps(
                    {
                        "brand_profile_id": str(profile_id),
                        "brand_key": profile_key,
                        "branding_hash": branding_hash,
                        "layout": layout_key,
                        "layout_version": layout_version,
                        "job_id": str(stored_job.id),
                        "job_status": stored_job.status.value,
                        "job_attempts": stored_job.attempts,
                        "job_error": stored_job.last_error,
                        "render_run_id": str(run.id) if run else None,
                        "render_run_status": run.status.value if run else None,
                        "render_run_error": run.sanitized_error if run else None,
                        "render_run_layout": run.layout_key if run else None,
                        "render_run_layout_version": run.layout_version if run else None,
                        "catalog_artifact_id": str(artifact.id) if artifact else None,
                        "pdf_path": artifact.file_path if artifact else None,
                        "checksum_sha256": (
                            artifact.checksum_sha256 if artifact else None
                        ),
                        "page_count": artifact.page_count if artifact else None,
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
