import argparse
import json
import os
import uuid

from sqlalchemy.orm import Session

from app.db.session import DATABASE_URL, create_sqlite_engine
from app.domain.schemas import CatalogSnapshotCreate
from app.services.catalog_snapshots import create_catalog_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one immutable CatalogSnapshot from current canonical state."
    )
    parser.add_argument("--product-id", required=True, action="append", type=uuid.UUID)
    parser.add_argument("--currency", default="PYG")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    try:
        with Session(engine, expire_on_commit=False) as session:
            snapshot = create_catalog_snapshot(
                session,
                CatalogSnapshotCreate(
                    product_ids=args.product_id,
                    currency=args.currency,
                ),
            )
            session.commit()
            print(
                json.dumps(
                    {
                        "snapshot_id": str(snapshot.id),
                        "content_hash": snapshot.content_hash,
                        "currency": snapshot.currency,
                        "as_of": snapshot.as_of.isoformat(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
