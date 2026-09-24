"""Run only the existing image.enhance.v1 durable-job handler."""

import argparse
import os
import time
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from app.ai.openai_image_enhancement import OpenAIImageEnhancementProvider
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.services.image_enhancement import IMAGE_ENHANCEMENT_JOB_TYPE
from app.workers.image_enhancement_handler import ImageEnhancementJobHandler
from app.workers.job_worker import JobWorker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local image enhancement worker.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--stale-after-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.poll_interval <= 0 or args.stale_after_seconds <= 0:
        parser.error("poll interval and stale timeout must be positive")
    provider = OpenAIImageEnhancementProvider.from_environment()
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    worker = JobWorker(factory, {IMAGE_ENHANCEMENT_JOB_TYPE: ImageEnhancementJobHandler(factory, provider)}, accepted_job_types={IMAGE_ENHANCEMENT_JOB_TYPE})
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
