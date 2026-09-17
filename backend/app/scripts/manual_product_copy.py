import argparse
import json
import os
import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import ProductCopyRun
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.domain.enums import ProductCopyReviewDecision
from app.domain.schemas import ProductCopyReviewRequest, ProductCopyRunRead
from app.services.product_copy_review import (
    apply_product_copy_review,
    resolve_effective_product_copy,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and review Product copy proposals."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list")
    listing.add_argument("--product-id", required=True, type=uuid.UUID)
    for command in ("approve", "reject"):
        review = subparsers.add_parser(command)
        review.add_argument("--run-id", required=True, type=uuid.UUID)
    correction = subparsers.add_parser("correct")
    correction.add_argument("--run-id", required=True, type=uuid.UUID)
    correction.add_argument("--text", required=True)
    resolution = subparsers.add_parser("resolve")
    resolution.add_argument("--product-id", required=True, type=uuid.UUID)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            if args.command == "list":
                runs = session.scalars(
                    select(ProductCopyRun)
                    .where(ProductCopyRun.product_id == args.product_id)
                    .order_by(ProductCopyRun.created_at.desc(), ProductCopyRun.id.desc())
                ).all()
                result = [
                    ProductCopyRunRead.model_validate(run).model_dump(mode="json")
                    for run in runs
                ]
            elif args.command == "resolve":
                result = resolve_effective_product_copy(
                    session, args.product_id
                ).model_dump(mode="json")
            else:
                decision = {
                    "approve": ProductCopyReviewDecision.APPROVED,
                    "correct": ProductCopyReviewDecision.CORRECTED,
                    "reject": ProductCopyReviewDecision.REJECTED,
                }[args.command]
                review = apply_product_copy_review(
                    session,
                    ProductCopyReviewRequest(
                        product_copy_run_id=args.run_id,
                        decision=decision,
                        corrected_short_description=(
                            args.text if args.command == "correct" else None
                        ),
                    ),
                )
                session.commit()
                result = {
                    "review_id": str(review.id),
                    "run_id": str(review.product_copy_run_id),
                    "decision": review.decision.value,
                    "corrected_short_description": (
                        review.corrected_short_description
                    ),
                    "applied_at": (
                        review.applied_at.isoformat() if review.applied_at else None
                    ),
                }
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
