import argparse
import time
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from app.db.session import create_sqlite_engine, effective_database_url
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.services.catalog_rendering import CATALOG_RENDER_JOB_TYPE_V2, CATALOG_RENDER_JOB_TYPE_V3, CATALOG_RENDER_JOB_TYPE_V4, CATALOG_RENDER_JOB_TYPE_V5
from app.workers.catalog_render_handler import catalog_render_handlers
from app.workers.job_worker import JobWorker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local catalog.render.v2/v3/v4/v5 durable-job worker."
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--stale-after-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.poll_interval <= 0:
        raise SystemExit("--poll-interval must be positive")
    if args.stale_after_seconds <= 0:
        raise SystemExit("--stale-after-seconds must be positive")

    engine = create_sqlite_engine(effective_database_url())
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    worker = JobWorker(
        session_factory,
        catalog_render_handlers(session_factory, ChromiumCatalogPdfRenderer()),
        accepted_job_types={CATALOG_RENDER_JOB_TYPE_V2, CATALOG_RENDER_JOB_TYPE_V3, CATALOG_RENDER_JOB_TYPE_V4, CATALOG_RENDER_JOB_TYPE_V5},
    )
    try:
        worker.recover_stale_jobs(timedelta(seconds=args.stale_after_seconds))
        if args.once:
            worker.run_once()
            return
        while True:
            if worker.run_once() is None:
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
