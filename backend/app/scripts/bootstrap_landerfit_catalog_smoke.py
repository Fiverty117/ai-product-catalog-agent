import json
import os
import sys
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Brand,
    Category,
    DerivedImage,
    ExtractionIdentityResolution,
    ExtractionRun,
    Photo,
    Price,
    Product,
    SKU,
)
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.db.types import utc_now
from app.domain.enums import (
    DerivedImageReviewDecision,
    DerivedImageReviewState,
    ExtractionRunStatus,
    ObservationState,
    PhotoRole,
)
from app.domain.identity import identity_key_v1
from app.domain.schemas import (
    CatalogSnapshotCreate,
    DerivedImageReviewCreate,
    ExtractionIdentityResolutionRequest,
    PriceCreate,
    ProductCatalogReadiness,
    ProductExtractionResult,
    SKUCreate,
)
from app.services.catalog_readiness import evaluate_product_catalog_readiness
from app.services.catalog_snapshots import create_catalog_snapshot
from app.services.categories import (
    assign_product_category,
    create_category,
    set_category_active,
)
from app.services.identity_resolution import (
    find_brand_identity_candidates,
    find_product_identity_candidates,
    resolve_extraction_identity,
)
from app.services.image_presentation import (
    create_derived_image_review,
    get_derived_image_review_state,
    select_derived_image_for_photo,
)
from app.services.photo_ownership import assign_photo_to_product
from app.services.prices import select_active_approved_price
from app.services.sku_variants import (
    create_manual_sku,
    find_sku_variant_candidates,
)

EXTRACTION_RUN_ID = uuid.UUID("6ccf4eb3-a840-47af-a0dd-0e8bda3e43a0")
SOURCE_PHOTO_ID = uuid.UUID("d7e7c758-a886-45bf-9d42-42800e54f642")
DERIVED_IMAGE_ID = uuid.UUID("2a1043f2-df06-4c6d-ad0a-1945d6b0aeea")

BRAND_NAME = "Landerfit"
PRODUCT_NAME = "Premium Whey"
CATEGORY_NAME = "Proteínas"
CATEGORY_SORT_ORDER = 100
PRICE_AMOUNT = Decimal("360000")
CURRENCY = "PYG"
PRICE_SOURCE = "manual-bootstrap-landerfit-catalog-smoke-v1"


class CatalogSmokeBootstrapError(RuntimeError):
    pass


class CatalogSmokeNotReadyError(CatalogSmokeBootstrapError):
    def __init__(self, report: ProductCatalogReadiness):
        self.report = report
        super().__init__("Product is not catalog-ready")


def main() -> None:
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            try:
                output = bootstrap_landerfit_catalog_smoke(session)
                session.commit()
            except CatalogSmokeNotReadyError as error:
                output = {
                    "readiness": _readiness_summary(session, error.report),
                    "snapshot": None,
                }
                session.rollback()
                print(
                    json.dumps(
                        output,
                        indent=2,
                        sort_keys=True,
                    )
                )
                raise SystemExit(1) from None
            except (CatalogSmokeBootstrapError, ValueError) as error:
                session.rollback()
                print(
                    json.dumps({"error": str(error)}, indent=2, sort_keys=True),
                    file=sys.stderr,
                )
                raise SystemExit(1) from None
            except Exception:
                session.rollback()
                raise
        print(json.dumps(output, indent=2, sort_keys=True))
    finally:
        engine.dispose()


def bootstrap_landerfit_catalog_smoke(session: Session) -> dict[str, object]:
    """Create one reviewed canonical Product and immutable snapshot.

    Transaction ownership remains with the caller. This function flushes as needed
    but never commits.
    """

    as_of = utc_now()
    extraction_run = _require_extraction_evidence(session)
    brand, product = _resolve_or_reuse_identity(session, extraction_run)
    sku = _create_or_reuse_sku(session, product)
    photo = _assign_source_photo(session, extraction_run, product)
    category = _create_or_reuse_category(session)
    assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    price = _create_or_reuse_price(session, sku=sku, as_of=as_of)
    derived = _approve_and_select_derived_image(session, photo)
    session.flush()

    readiness = evaluate_product_catalog_readiness(
        session,
        product_id=product.id,
        currency=CURRENCY,
        as_of=as_of,
    )
    if not readiness.is_ready:
        raise CatalogSmokeNotReadyError(readiness)
    if sku.id not in readiness.ready_sku_ids:
        raise CatalogSmokeBootstrapError(
            "The smoke SKU was not selected as publishable by Catalog Readiness"
        )

    snapshot = create_catalog_snapshot(
        session,
        CatalogSnapshotCreate(
            product_ids=[product.id],
            currency=CURRENCY,
            as_of=as_of,
        ),
    )
    return {
        "readiness": _readiness_summary(session, readiness),
        "snapshot": {
            "snapshot_id": str(snapshot.id),
            "content_hash": snapshot.content_hash,
            "currency": snapshot.currency,
            "as_of": snapshot.as_of.isoformat(),
            "product_id": str(product.id),
            "sku_id": str(sku.id),
            "price_id": str(price.id),
            "source_photo_id": str(photo.id),
            "selected_derived_image_id": str(derived.id),
        },
    }


def _require_extraction_evidence(session: Session) -> ExtractionRun:
    run = session.get(ExtractionRun, EXTRACTION_RUN_ID)
    if run is None:
        raise CatalogSmokeBootstrapError(
            f"ExtractionRun not found: {EXTRACTION_RUN_ID}"
        )
    if run.status is not ExtractionRunStatus.SUCCEEDED:
        raise CatalogSmokeBootstrapError(
            f"ExtractionRun must be succeeded: {run.id} ({run.status.value})"
        )
    try:
        result = ProductExtractionResult.model_validate(run.structured_result)
    except ValidationError as error:
        raise CatalogSmokeBootstrapError(
            "ExtractionRun has invalid structured_result"
        ) from error

    if (
        result.brand_name.value is None
        or identity_key_v1(result.brand_name.value)
        != identity_key_v1(BRAND_NAME)
        or result.product_name.value is None
        or identity_key_v1(result.product_name.value)
        != identity_key_v1(PRODUCT_NAME)
        or result.flavor.value is None
        or result.flavor.value.casefold() != "vanilla"
        or result.size_value.value != Decimal("2")
        or result.size_unit.value is None
        or result.size_unit.value.casefold() != "lb"
        or result.servings.value is not None
        or result.servings.state is not ObservationState.NOT_PRESENT
    ):
        raise CatalogSmokeBootstrapError(
            "ExtractionRun evidence does not match the Landerfit smoke fixture"
        )
    return run


def _resolve_or_reuse_identity(
    session: Session,
    run: ExtractionRun,
) -> tuple[Brand, Product]:
    existing = session.scalar(
        select(ExtractionIdentityResolution).where(
            ExtractionIdentityResolution.extraction_run_id == run.id
        )
    )
    if existing is not None:
        brand = existing.brand
        product = existing.product
        if (
            brand.identity_key != identity_key_v1(BRAND_NAME)
            or product.identity_key != identity_key_v1(PRODUCT_NAME)
            or product.brand_id != brand.id
        ):
            raise CatalogSmokeBootstrapError(
                "Existing ExtractionRun identity resolution conflicts with the "
                "Landerfit smoke identity"
            )
        return brand, product

    brand_candidates = find_brand_identity_candidates(session, BRAND_NAME)
    exact_brands = [
        brand
        for brand in brand_candidates
        if brand.identity_key == identity_key_v1(BRAND_NAME)
    ]
    if len(exact_brands) > 1:
        raise CatalogSmokeBootstrapError("Multiple exact Brand candidates found")
    if exact_brands:
        brand = exact_brands[0]
        brand_decision = {"action": "use_existing", "brand_id": brand.id}
        product_candidates = find_product_identity_candidates(
            session,
            brand_id=brand.id,
            name=PRODUCT_NAME,
        )
        exact_products = [
            product
            for product in product_candidates
            if product.identity_key == identity_key_v1(PRODUCT_NAME)
        ]
        if len(exact_products) > 1:
            raise CatalogSmokeBootstrapError(
                "Multiple exact Product candidates found"
            )
        if exact_products:
            product_decision = {
                "action": "use_existing",
                "product_id": exact_products[0].id,
            }
        elif product_candidates:
            raise CatalogSmokeBootstrapError(
                "Only loose Product identity candidates exist; resolve them "
                "explicitly before rerunning this script"
            )
        else:
            product_decision = {"action": "create_new", "name": PRODUCT_NAME}
    elif brand_candidates:
        raise CatalogSmokeBootstrapError(
            "Only loose Brand identity candidates exist; resolve them explicitly "
            "before rerunning this script"
        )
    else:
        brand_decision = {"action": "create_new", "name": BRAND_NAME}
        product_decision = {"action": "create_new", "name": PRODUCT_NAME}

    resolution = resolve_extraction_identity(
        session,
        ExtractionIdentityResolutionRequest(
            extraction_run_id=run.id,
            brand=brand_decision,
            product=product_decision,
        ),
    )
    return resolution.brand, resolution.product


def _create_or_reuse_sku(session: Session, product: Product) -> SKU:
    request = SKUCreate(
        product_id=product.id,
        external_sku=None,
        flavor="Vanilla",
        size_value=Decimal("2"),
        size_unit="LB",
        servings=None,
    )
    candidates = find_sku_variant_candidates(session, request)
    if len(candidates.exact) > 1:
        raise CatalogSmokeBootstrapError(
            "Multiple exact SKU variants exist; select one explicitly before "
            "rerunning this script"
        )
    if candidates.exact:
        return candidates.exact[0]
    if candidates.partial or candidates.external_sku:
        raise CatalogSmokeBootstrapError(
            "Only non-exact SKU candidates exist; resolve them explicitly before "
            "rerunning this script"
        )
    return create_manual_sku(session, request)


def _assign_source_photo(
    session: Session,
    run: ExtractionRun,
    product: Product,
) -> Photo:
    photo = session.get(Photo, SOURCE_PHOTO_ID)
    if photo is None:
        raise CatalogSmokeBootstrapError(f"Photo not found: {SOURCE_PHOTO_ID}")
    if not photo.is_original or photo.role is not PhotoRole.FRONT:
        raise CatalogSmokeBootstrapError(
            "Smoke Photo must remain an original front Photo"
        )
    if all(run_photo.id != photo.id for run_photo in run.photos):
        raise CatalogSmokeBootstrapError(
            "Smoke Photo is not historical input evidence for the ExtractionRun"
        )
    return assign_photo_to_product(
        session,
        photo_id=photo.id,
        product_id=product.id,
    )


def _create_or_reuse_category(session: Session) -> Category:
    category = session.scalar(
        select(Category).where(
            Category.identity_key == identity_key_v1(CATEGORY_NAME)
        )
    )
    if category is None:
        return create_category(
            session,
            name=CATEGORY_NAME,
            sort_order=CATEGORY_SORT_ORDER,
            is_active=True,
        )
    if category.sort_order != CATEGORY_SORT_ORDER:
        category.sort_order = CATEGORY_SORT_ORDER
    if not category.is_active:
        set_category_active(session, category_id=category.id, is_active=True)
    session.flush()
    return category


def _create_or_reuse_price(
    session: Session,
    *,
    sku: SKU,
    as_of: datetime,
) -> Price:
    active = select_active_approved_price(
        session,
        sku_id=sku.id,
        currency=CURRENCY,
        as_of=as_of,
    )
    if active is not None:
        if active.amount != PRICE_AMOUNT:
            raise CatalogSmokeBootstrapError(
                f"SKU already has a different active approved {CURRENCY} Price: "
                f"{active.id} ({active.amount})"
            )
        return active

    request = PriceCreate(
        sku_id=sku.id,
        amount=PRICE_AMOUNT,
        currency=CURRENCY,
        valid_from=as_of,
        source=PRICE_SOURCE,
        approved=True,
    )
    price = Price(
        sku=sku,
        amount=request.amount,
        currency=request.currency,
        valid_from=request.valid_from,
        source=request.source,
        approved=request.approved,
    )
    session.add(price)
    session.flush()
    selected = select_active_approved_price(
        session,
        sku_id=sku.id,
        currency=CURRENCY,
        as_of=as_of,
    )
    if selected is None or selected.id != price.id:
        raise CatalogSmokeBootstrapError(
            "New smoke Price was not selected as the active approved Price"
        )
    return price


def _approve_and_select_derived_image(
    session: Session,
    photo: Photo,
) -> DerivedImage:
    derived = session.get(DerivedImage, DERIVED_IMAGE_ID)
    if derived is None:
        raise CatalogSmokeBootstrapError(
            f"DerivedImage not found: {DERIVED_IMAGE_ID}"
        )
    if derived.source_photo_id != photo.id:
        raise CatalogSmokeBootstrapError(
            "DerivedImage does not belong to the smoke source Photo"
        )
    review_state = get_derived_image_review_state(session, derived.id)
    if review_state is DerivedImageReviewState.REJECTED:
        raise CatalogSmokeBootstrapError(
            "DerivedImage is currently human-rejected; it will not be overridden"
        )
    if review_state is DerivedImageReviewState.UNREVIEWED:
        create_derived_image_review(
            session,
            DerivedImageReviewCreate(
                derived_image_id=derived.id,
                decision=DerivedImageReviewDecision.APPROVED,
            ),
        )
    select_derived_image_for_photo(
        session,
        photo_id=photo.id,
        derived_image_id=derived.id,
    )
    return derived


def _readiness_summary(
    session: Session,
    report: ProductCatalogReadiness,
) -> dict[str, object]:
    primary_category = (
        session.get(Category, report.primary_category_id)
        if report.primary_category_id is not None
        else None
    )
    return {
        "product_id": str(report.product_id),
        "is_ready": report.is_ready,
        "primary_category": (
            {
                "category_id": str(primary_category.id),
                "name": primary_category.name,
            }
            if primary_category is not None
            else None
        ),
        "hero_photo_id": (
            str(report.hero_photo_id) if report.hero_photo_id is not None else None
        ),
        "hero_presentation_type": (
            report.hero_presentation_type.value
            if report.hero_presentation_type is not None
            else None
        ),
        "hero_derived_image_id": (
            str(report.hero_derived_image_id)
            if report.hero_derived_image_id is not None
            else None
        ),
        "ready_sku_ids": [str(sku_id) for sku_id in report.ready_sku_ids],
        "blockers": [item.model_dump(mode="json") for item in report.blockers],
        "warnings": [item.model_dump(mode="json") for item in report.warnings],
        "presentation_warnings": [
            item.value for item in report.presentation_warnings
        ],
    }


if __name__ == "__main__":
    main()
