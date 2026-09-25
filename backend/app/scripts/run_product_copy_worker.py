import argparse
import time
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from app.ai.openai_product_copy import OpenAIProductCopyProvider
from app.db.session import create_sqlite_engine, effective_database_url
from app.services.product_copy import PRODUCT_COPY_JOB_TYPE
from app.workers.job_worker import JobWorker
from app.workers.product_copy_handler import ProductCopyJobHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local Product Copy durable-job worker."
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

    provider = OpenAIProductCopyProvider.from_environment()
    engine = create_sqlite_engine(effective_database_url())
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    worker = JobWorker(
        session_factory,
        {PRODUCT_COPY_JOB_TYPE: ProductCopyJobHandler(session_factory, provider)},
        accepted_job_types={PRODUCT_COPY_JOB_TYPE},
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
