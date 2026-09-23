"""Run existing product.extract.v1 handler for queued Product Intake jobs."""

import argparse
import os
import time
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from app.ai.openai_vision import OpenAIVisionProvider
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.services.extraction import PRODUCT_EXTRACTION_JOB_TYPE
from app.workers.extraction_handler import ProductExtractionJobHandler
from app.workers.job_worker import JobWorker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local product extraction worker.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--stale-after-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.poll_interval <= 0 or args.stale_after_seconds <= 0:
        parser.error("poll interval and stale timeout must be positive")
    provider = OpenAIVisionProvider.from_environment()
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    worker = JobWorker(
        factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(factory, provider)},
        accepted_job_types={PRODUCT_EXTRACTION_JOB_TYPE},
    )
    try:
        worker.recover_stale_jobs(timedelta(seconds=args.stale_after_seconds))
        if args.once:
            worker.run_once()
        else:
            while True:
                if worker.run_once() is None:
                    time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
