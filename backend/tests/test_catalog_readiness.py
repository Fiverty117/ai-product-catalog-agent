import uuid
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Brand,
    Category,
    CategorySuggestionReview,
    CategorySuggestionRun,
    ExtractionFieldReview,
    ExtractionRun,
    Photo,
    Price,
    Product,
    ProductCategory,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import (
    CatalogHeroPhotoSource,
    CatalogReadinessIssueCode,
    CategorySuggestionReviewDecision,
    FieldSource,
    PhotoRole,
)
from app.domain.schemas import (
    CategorySuggestionJobPayload,
    CategorySuggestionReviewRequest,
)
from app.services.catalog_readiness import evaluate_product_catalog_readiness
from app.services.categories import (
    assign_product_category,
    create_category,
    set_category_active,
)
from app.services.category_suggestion_review import apply_category_suggestion_review
from app.services.category_suggestions import (
    build_category_suggestion_input_hash,
    build_category_suggestion_input_snapshot,
    create_running_category_suggestion_run,
    mark_category_suggestion_run_succeeded,
)


AS_OF = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
_PHOTO_MARKERS = count(1)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'readiness.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.info["asset_dir"] = tmp_path / "assets"
        yield session
    engine.dispose()


def make_product(session: Session, *, name: str = "Catalog Product") -> Product:
    product = Product(name=name, brand=Brand(name=f"Brand {uuid.uuid4()}"))
    session.add(product)
    session.flush()
    return product


def make_sku(
    session: Session,
    product: Product,
    *,
    sku_id: uuid.UUID | None = None,
    flavor: str | None = None,
) -> SKU:
    sku = SKU(id=sku_id, product=product, flavor=flavor)
    session.add(sku)
    session.flush()
    return sku


def add_price(
    session: Session,
    sku: SKU,
    *,
    amount: str = "10.0000",
    currency: str = "USD",
    valid_from: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc),
    approved: bool = True,
    created_at: datetime | None = None,
) -> Price:
    price = Price(
        sku=sku,
        amount=Decimal(amount),
        currency=currency,
        valid_from=valid_from,
        source="manual",
        approved=approved,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(price)
    session.flush()
    return price


def add_photo(
    session: Session,
    *,
    product: Product | None = None,
    sku: SKU | None = None,
    role: PhotoRole = PhotoRole.FRONT,
    available: bool = True,
    created_at: datetime | None = None,
) -> Photo:
    marker = next(_PHOTO_MARKERS)
    path = session.info["asset_dir"] / f"photo-{marker}.jpg"
    if available:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"catalog-photo")
    photo = Photo(
        product=product,
        sku=sku,
        file_path=str(path),
        checksum_sha256=f"{marker:064x}",
        original_filename=path.name,
        mime_type="image/jpeg",
        file_size_bytes=13,
        width=10,
        height=10,
        role=role,
        is_original=True,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(photo)
    session.flush()
    return photo


def assign_primary(session: Session, product: Product) -> Category:
    category = create_category(session, name=f"Primary {uuid.uuid4()}")
    assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    return category


def make_ready_context(session: Session):
    product = make_product(session)
    category = assign_primary(session, product)
    sku = make_sku(session, product)
    price = add_price(session, sku)
    photo = add_photo(session, product=product)
    return product, category, sku, price, photo


def blocker_codes(report) -> list[CatalogReadinessIssueCode]:
    return [issue.code for issue in report.blockers]


def warning_codes(report) -> list[CatalogReadinessIssueCode]:
    return [issue.code for issue in report.warnings]


def test_ready_product_and_dimensionless_sku_need_no_ai_history(
    session: Session,
) -> None:
    product, category, sku, price, photo = make_ready_context(session)

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="usd", as_of=AS_OF
    )

    assert report.is_ready is True
    assert report.primary_category_id == category.id
    assert report.hero_photo_id == photo.id
    assert report.hero_photo_source is CatalogHeroPhotoSource.PRODUCT
    assert report.hero_source_sku_id is None
    assert report.ready_sku_ids == [sku.id]
    assert report.sku_reports[0].active_price_id == price.id
    assert report.sku_reports[0].active_price_amount == Decimal("10.0000")
    assert report.currency == "USD"
    assert sku.flavor is sku.size_value is sku.size_unit is sku.servings is None
    assert session.scalar(select(func.count()).select_from(ExtractionRun)) == 0
    assert session.scalar(select(func.count()).select_from(ExtractionFieldReview)) == 0
    assert session.scalar(select(func.count()).select_from(CategorySuggestionRun)) == 0
    assert session.scalar(select(func.count()).select_from(CategorySuggestionReview)) == 0


def test_missing_inactive_and_secondary_only_category_diagnostics(
    session: Session,
) -> None:
    missing = make_product(session, name="Missing primary")
    missing_sku = make_sku(session, missing)
    add_price(session, missing_sku)
    add_photo(session, product=missing)

    secondary_only = make_product(session, name="Secondary only")
    secondary_sku = make_sku(session, secondary_only)
    add_price(session, secondary_sku)
    add_photo(session, product=secondary_only)
    secondary = create_category(session, name=f"Secondary {uuid.uuid4()}")
    assign_product_category(
        session,
        product_id=secondary_only.id,
        category_id=secondary.id,
    )

    inactive = make_product(session, name="Inactive primary")
    inactive_sku = make_sku(session, inactive)
    add_price(session, inactive_sku)
    add_photo(session, product=inactive)
    inactive_primary = assign_primary(session, inactive)
    set_category_active(
        session, category_id=inactive_primary.id, is_active=False
    )

    missing_report = evaluate_product_catalog_readiness(
        session, product_id=missing.id, currency="USD", as_of=AS_OF
    )
    secondary_report = evaluate_product_catalog_readiness(
        session, product_id=secondary_only.id, currency="USD", as_of=AS_OF
    )
    inactive_report = evaluate_product_catalog_readiness(
        session, product_id=inactive.id, currency="USD", as_of=AS_OF
    )

    assert blocker_codes(missing_report) == [
        CatalogReadinessIssueCode.MISSING_PRIMARY_CATEGORY
    ]
    assert CatalogReadinessIssueCode.MISSING_PRIMARY_CATEGORY in blocker_codes(
        secondary_report
    )
    assert blocker_codes(inactive_report) == [
        CatalogReadinessIssueCode.INACTIVE_PRIMARY_CATEGORY
    ]
    assert inactive_report.primary_category_id == inactive_primary.id


def test_inactive_secondary_category_is_warning_only(session: Session) -> None:
    product, *_ = make_ready_context(session)
    first = create_category(session, name="Inactive B", sort_order=20)
    second = create_category(session, name="Inactive A", sort_order=10)
    for category in (first, second):
        assign_product_category(
            session, product_id=product.id, category_id=category.id
        )
        set_category_active(session, category_id=category.id, is_active=False)

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.is_ready is True
    assert warning_codes(report) == [
        CatalogReadinessIssueCode.INACTIVE_SECONDARY_CATEGORY,
        CatalogReadinessIssueCode.INACTIVE_SECONDARY_CATEGORY,
    ]
    assert [issue.category_id for issue in report.warnings] == [second.id, first.id]


def test_product_without_skus_has_precise_blocker(session: Session) -> None:
    product = make_product(session)
    assign_primary(session, product)
    add_photo(session, product=product)

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.is_ready is False
    assert blocker_codes(report) == [CatalogReadinessIssueCode.NO_SKUS]
    assert report.sku_reports == []
    assert report.ready_sku_ids == []


def test_unpriced_sku_is_visible_but_does_not_block_priced_sibling(
    session: Session,
) -> None:
    product, _, ready_sku, *_ = make_ready_context(session)
    excluded_sku = make_sku(session, product, flavor="Unpriced")

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )
    reports = {item.sku_id: item for item in report.sku_reports}

    assert report.is_ready is True
    assert report.ready_sku_ids == sorted([ready_sku.id], key=str)
    assert reports[excluded_sku.id].is_publishable is False
    assert [issue.code for issue in reports[excluded_sku.id].blockers] == [
        CatalogReadinessIssueCode.MISSING_ACTIVE_APPROVED_PRICE
    ]
    assert CatalogReadinessIssueCode.SKU_EXCLUDED_MISSING_ACTIVE_PRICE in warning_codes(
        report
    )


@pytest.mark.parametrize(
    "price_values",
    [
        {"approved": False},
        {"currency": "EUR"},
        {"valid_from": datetime(2026, 10, 1, tzinfo=timezone.utc)},
    ],
)
def test_non_active_prices_leave_product_without_publishable_skus(
    session: Session, price_values: dict
) -> None:
    product = make_product(session)
    assign_primary(session, product)
    sku = make_sku(session, product)
    add_price(session, sku, **price_values)
    add_photo(session, product=product)

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.is_ready is False
    assert CatalogReadinessIssueCode.NO_PUBLISHABLE_SKUS in blocker_codes(report)
    assert report.sku_reports[0].active_price_id is None


def test_future_price_and_requested_currency_are_evaluated_at_as_of(
    session: Session,
) -> None:
    product = make_product(session)
    assign_primary(session, product)
    sku = make_sku(session, product)
    future = add_price(
        session,
        sku,
        amount="12.0000",
        currency="EUR",
        valid_from=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    add_photo(session, product=product)

    before = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="EUR", as_of=AS_OF
    )
    after = evaluate_product_catalog_readiness(
        session,
        product_id=product.id,
        currency="EUR",
        as_of=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    usd = evaluate_product_catalog_readiness(
        session,
        product_id=product.id,
        currency="USD",
        as_of=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert before.is_ready is False
    assert after.is_ready is True
    assert after.sku_reports[0].active_price_id == future.id
    assert usd.is_ready is False


def test_latest_applicable_price_uses_valid_from_then_created_at(
    session: Session,
) -> None:
    product = make_product(session)
    assign_primary(session, product)
    sku = make_sku(session, product)
    add_photo(session, product=product)
    add_price(
        session,
        sku,
        amount="9.0000",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    add_price(
        session,
        sku,
        amount="11.0000",
        valid_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    selected = add_price(
        session,
        sku,
        amount="12.0000",
        valid_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.sku_reports[0].active_price_id == selected.id
    assert report.sku_reports[0].active_price_amount == Decimal("12.0000")


def test_product_front_is_preferred_over_sku_front(session: Session) -> None:
    product, _, sku, *_ = make_ready_context(session)
    product_front = product.photos[0]
    sku_front = add_photo(
        session,
        sku=sku,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.hero_photo_id == product_front.id
    assert report.hero_photo_id != sku_front.id
    assert report.hero_photo_source is CatalogHeroPhotoSource.PRODUCT


def test_sku_front_can_represent_product_with_sibling_variants(
    session: Session,
) -> None:
    product = make_product(session)
    assign_primary(session, product)
    first = make_sku(session, product, sku_id=uuid.UUID(int=1), flavor="Vanilla")
    second = make_sku(session, product, sku_id=uuid.UUID(int=2), flavor="Chocolate")
    add_price(session, first)
    add_price(session, second)
    add_photo(session, product=product, available=False)
    first_front = add_photo(
        session,
        sku=first,
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    add_photo(
        session,
        sku=second,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.is_ready is True
    assert report.hero_photo_id == first_front.id
    assert report.hero_photo_source is CatalogHeroPhotoSource.SKU
    assert report.hero_source_sku_id == first.id
    assert report.ready_sku_ids == [first.id, second.id]


def test_hero_choice_is_stable_and_skips_missing_candidate(session: Session) -> None:
    product, *_ = make_ready_context(session)
    original = product.photos[0]
    original.file_path = str(session.info["asset_dir"] / "now-missing.jpg")
    earlier_missing = add_photo(
        session,
        product=product,
        available=False,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    available = add_photo(
        session,
        product=product,
        created_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
    )
    session.flush()

    first = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )
    second = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert first == second
    assert first.hero_photo_id == available.id
    assert first.hero_photo_id != earlier_missing.id


def test_missing_front_record_and_missing_asset_have_distinct_codes(
    session: Session,
) -> None:
    no_front, *_ = make_ready_context(session)
    no_front.photos[0].role = PhotoRole.BACK
    missing_asset, *_ = make_ready_context(session)
    missing_asset.photos[0].file_path = str(
        session.info["asset_dir"] / "missing-front.jpg"
    )
    session.flush()

    no_front_report = evaluate_product_catalog_readiness(
        session, product_id=no_front.id, currency="USD", as_of=AS_OF
    )
    missing_asset_report = evaluate_product_catalog_readiness(
        session, product_id=missing_asset.id, currency="USD", as_of=AS_OF
    )

    assert CatalogReadinessIssueCode.MISSING_CATALOG_FRONT_PHOTO in blocker_codes(
        no_front_report
    )
    assert CatalogReadinessIssueCode.MISSING_CATALOG_PHOTO_ASSET in blocker_codes(
        missing_asset_report
    )


def test_readiness_is_read_only_and_deterministically_ordered(session: Session) -> None:
    product, category, _, price, photo = make_ready_context(session)
    low = make_sku(session, product, sku_id=uuid.UUID(int=10))
    high = make_sku(session, product, sku_id=uuid.UUID(int=20))
    add_price(session, high)
    session.flush()
    before_counts = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in (Brand, Product, SKU, Category, ProductCategory, Photo, Price)
    }
    before_state = (
        product.name,
        product.updated_at,
        category.is_active,
        price.approved,
        photo.product_id,
    )

    first = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )
    second = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )
    after_counts = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in before_counts
    }

    assert first == second
    assert [item.sku_id for item in first.sku_reports] == sorted(
        [item.sku_id for item in first.sku_reports]
    )
    assert first.ready_sku_ids == sorted(first.ready_sku_ids)
    assert [warning.sku_id for warning in first.warnings] == [low.id]
    assert before_counts == after_counts
    assert before_state == (
        product.name,
        product.updated_at,
        category.is_active,
        price.approved,
        photo.product_id,
    )
    assert not session.new and not session.dirty and not session.deleted


def test_accepted_empty_ai_suggestion_does_not_replace_canonical_readiness(
    session: Session,
) -> None:
    product = make_product(session)
    sku = make_sku(session, product)
    add_price(session, sku)
    add_photo(session, product=product)
    snapshot = build_category_suggestion_input_snapshot(session, product.id)
    parameters = {"reasoning_effort": "low"}
    input_hash = build_category_suggestion_input_hash(
        input_snapshot=snapshot,
        provider="openai",
        model="gpt-5.6-sol",
        prompt_version="product-category-v1",
        schema_version="product-category-result-v1",
        parameters=parameters,
    )
    run = create_running_category_suggestion_run(
        session,
        payload=CategorySuggestionJobPayload(
            product_id=product.id,
            provider="openai",
            model="gpt-5.6-sol",
            prompt_version="product-category-v1",
            schema_version="product-category-result-v1",
            parameters=parameters,
            input_hash=input_hash,
            input_snapshot=snapshot,
        ),
    )
    mark_category_suggestion_run_succeeded(
        session,
        run,
        structured_result={"primary": None, "secondary": []},
    )
    apply_category_suggestion_review(
        session,
        CategorySuggestionReviewRequest(
            category_suggestion_run_id=run.id,
            decision=CategorySuggestionReviewDecision.ACCEPTED,
        ),
    )

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert session.scalar(select(func.count()).select_from(CategorySuggestionReview)) == 1
    assert session.scalar(select(func.count()).select_from(ProductCategory)) == 0
    assert report.is_ready is False
    assert CatalogReadinessIssueCode.MISSING_PRIMARY_CATEGORY in blocker_codes(report)
