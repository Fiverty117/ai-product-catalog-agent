import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import (
    Base,
    Brand,
    ExtractionIdentityResolution,
    ExtractionRun,
    Product,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus, IdentityResolutionAction
from app.domain.identity import InvalidIdentityNameError, identity_key_v1
from app.domain.schemas import (
    ExtractionIdentityResolutionRead,
    ExtractionIdentityResolutionRequest,
    ProductExtractionResult,
)
from app.services.identity_resolution import (
    DuplicateBrandIdentityError,
    DuplicateIdentityResolutionError,
    DuplicateProductIdentityError,
    ProductBrandMismatchError,
    SKUProductConflictError,
    UnreviewableIdentityExtractionRunError,
    find_brand_identity_candidates,
    find_product_identity_candidates,
    resolve_extraction_identity,
)


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def identity_store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


@pytest.fixture
def session(identity_store) -> Session:
    _, factory = identity_store
    with factory() as session:
        yield session


def observation(value, *, state="extracted"):
    return {
        "value": value,
        "confidence": "0.9" if value is not None else None,
        "evidence": None,
        "state": state,
    }


def extraction_result(**overrides) -> ProductExtractionResult:
    values = {
        "brand_name": observation("Observed Brand"),
        "product_name": observation("Observed Product"),
        "flavor": observation(None, state="not_present"),
        "size_value": observation(None, state="not_present"),
        "size_unit": observation(None, state="not_present"),
        "servings": observation(None, state="not_present"),
        **overrides,
    }
    return ProductExtractionResult.model_validate(values)


def make_run(
    session: Session,
    *,
    status: ExtractionRunStatus = ExtractionRunStatus.SUCCEEDED,
    sku: SKU | None = None,
    result: ProductExtractionResult | None = None,
) -> ExtractionRun:
    run = ExtractionRun(
        sku=sku,
        provider="test-provider",
        model="vision-model",
        prompt_version="product-extraction-v1",
        schema_version="product-result-v1",
        parameters_hash="a" * 64,
        status=status,
        started_at=NOW,
        completed_at=NOW if status is not ExtractionRunStatus.RUNNING else None,
        structured_result=(
            (result or extraction_result()).model_dump(mode="json")
            if status is ExtractionRunStatus.SUCCEEDED
            else None
        ),
        sanitized_error="failed" if status is ExtractionRunStatus.FAILED else None,
    )
    session.add(run)
    session.flush()
    return run


def make_brand_product(
    session: Session,
    *,
    brand_name: str = "Landerfit",
    product_name: str = "Premium Whey",
) -> tuple[Brand, Product]:
    brand = Brand(name=brand_name)
    product = Product(name=product_name, brand=brand)
    session.add(product)
    session.flush()
    return brand, product


def request(
    run: ExtractionRun,
    *,
    brand: dict,
    product: dict,
) -> ExtractionIdentityResolutionRequest:
    return ExtractionIdentityResolutionRequest(
        extraction_run_id=run.id,
        brand=brand,
        product=product,
    )


@pytest.mark.parametrize(
    "name",
    ["LANDERFIT", "Landerfit", "  landerfit  ", "\tLanderfit\n"],
)
def test_identity_key_v1_case_and_edge_whitespace_equivalence(name: str) -> None:
    assert identity_key_v1(name) == "landerfit"


def test_identity_key_v1_collapses_internal_whitespace() -> None:
    assert identity_key_v1("Premium   \t Whey") == "premium whey"


def test_identity_key_v1_keeps_meaningful_spaces() -> None:
    assert identity_key_v1("Landerfit") != identity_key_v1("Lander Fit")


def test_identity_key_v1_rejects_empty_names() -> None:
    with pytest.raises(InvalidIdentityNameError):
        identity_key_v1(" \t\n ")


def test_identity_key_v1_uses_nfkc_deterministically() -> None:
    assert identity_key_v1("ＬＡＮＤＥＲＦＩＴ") == identity_key_v1("Landerfit")


def test_explicitly_create_new_brand_and_product(session: Session) -> None:
    run = make_run(session)

    resolution = resolve_extraction_identity(
        session,
        request(
            run,
            brand={"action": "create_new", "name": "  LANDERFIT  "},
            product={"action": "create_new", "name": " Premium   Whey "},
        ),
        applied_at=NOW,
    )

    assert resolution.brand.name == "LANDERFIT"
    assert resolution.brand.identity_key == "landerfit"
    assert resolution.product.name == "Premium Whey"
    assert resolution.product.identity_key == "premium whey"
    assert resolution.product.brand is resolution.brand
    assert resolution.brand_action is IdentityResolutionAction.CREATE_NEW
    assert resolution.product_action is IdentityResolutionAction.CREATE_NEW
    assert resolution.applied_at == NOW
    assert run.identity_resolution is resolution
    assert ExtractionIdentityResolutionRead.model_validate(resolution).product_id == (
        resolution.product.id
    )


def test_explicitly_use_existing_brand_and_product(session: Session) -> None:
    brand, product = make_brand_product(session)
    run = make_run(session)

    resolution = resolve_extraction_identity(
        session,
        request(
            run,
            brand={"action": "use_existing", "brand_id": brand.id},
            product={"action": "use_existing", "product_id": product.id},
        ),
    )

    assert resolution.brand is brand
    assert resolution.product is product
    assert resolution.brand_action is IdentityResolutionAction.USE_EXISTING
    assert resolution.product_action is IdentityResolutionAction.USE_EXISTING
    assert brand.name == "Landerfit"
    assert product.name == "Premium Whey"


def test_create_new_rejects_existing_exact_brand_identity(session: Session) -> None:
    brand, _ = make_brand_product(session)
    run = make_run(session)

    with pytest.raises(
        DuplicateBrandIdentityError,
        match=f"use_existing with brand_id={brand.id}",
    ):
        resolve_extraction_identity(
            session,
            request(
                run,
                brand={"action": "create_new", "name": " LANDERFIT "},
                product={"action": "create_new", "name": "Other Product"},
            ),
        )

    assert run.identity_resolution is None


def test_database_enforces_global_brand_identity_uniqueness(
    session: Session,
) -> None:
    session.add_all([Brand(name="Landerfit"), Brand(name=" LANDERFIT ")])

    with pytest.raises(IntegrityError, match="brands.identity_key"):
        session.flush()


def test_database_enforces_global_brand_identity_uniqueness(session: Session) -> None:
    session.add_all([Brand(name="Landerfit"), Brand(name=" LANDERFIT ")])

    with pytest.raises(IntegrityError, match="brands.identity_key"):
        session.flush()


def test_loose_brand_candidate_is_suggestion_only(session: Session) -> None:
    brand, _ = make_brand_product(session, brand_name="Landerfit")
    brand_count = session.scalar(select(func.count()).select_from(Brand))

    candidates = find_brand_identity_candidates(session, "Lander Fit")

    assert candidates == [brand]
    assert identity_key_v1("Lander Fit") != brand.identity_key
    assert session.scalar(select(func.count()).select_from(Brand)) == brand_count
    assert session.scalar(
        select(func.count()).select_from(ExtractionIdentityResolution)
    ) == 0


def test_duplicate_product_identity_under_same_brand_is_rejected(
    session: Session,
) -> None:
    brand, product = make_brand_product(session)
    run = make_run(session)

    with pytest.raises(
        DuplicateProductIdentityError,
        match=f"use_existing with product_id={product.id}",
    ):
        resolve_extraction_identity(
            session,
            request(
                run,
                brand={"action": "use_existing", "brand_id": brand.id},
                product={"action": "create_new", "name": "PREMIUM WHEY"},
            ),
        )


def test_database_enforces_product_identity_uniqueness_within_brand(
    session: Session,
) -> None:
    brand = Brand(name="Landerfit")
    session.add_all(
        [
            Product(name="Premium Whey", brand=brand),
            Product(name=" PREMIUM   WHEY ", brand=brand),
        ]
    )

    with pytest.raises(IntegrityError, match="products.brand_id, products.identity_key"):
        session.flush()


def test_database_enforces_product_identity_uniqueness_within_brand(
    session: Session,
) -> None:
    brand = Brand(name="Landerfit")
    session.add_all(
        [
            Product(brand=brand, name="Premium Whey"),
            Product(brand=brand, name=" PREMIUM   WHEY "),
        ]
    )

    with pytest.raises(IntegrityError, match="products.brand_id, products.identity_key"):
        session.flush()


def test_same_product_identity_under_different_brands_is_allowed(
    session: Session,
) -> None:
    _, first_product = make_brand_product(session, brand_name="First Brand")
    second_brand = Brand(name="Second Brand")
    session.add(second_brand)
    session.flush()
    run = make_run(session)

    resolution = resolve_extraction_identity(
        session,
        request(
            run,
            brand={"action": "use_existing", "brand_id": second_brand.id},
            product={"action": "create_new", "name": "PREMIUM WHEY"},
        ),
    )

    assert resolution.product.identity_key == first_product.identity_key
    assert resolution.product.brand_id == second_brand.id
    assert resolution.product.id != first_product.id


def test_existing_product_from_wrong_brand_is_rejected(session: Session) -> None:
    first_brand, first_product = make_brand_product(session)
    second_brand = Brand(name="Second Brand")
    session.add(second_brand)
    session.flush()
    run = make_run(session)

    with pytest.raises(ProductBrandMismatchError):
        resolve_extraction_identity(
            session,
            request(
                run,
                brand={"action": "use_existing", "brand_id": second_brand.id},
                product={"action": "use_existing", "product_id": first_product.id},
            ),
        )

    assert first_product.brand_id == first_brand.id


def test_product_candidates_remain_brand_scoped(session: Session) -> None:
    first_brand, first_product = make_brand_product(session)
    second_brand, _ = make_brand_product(
        session,
        brand_name="Second Brand",
        product_name="Other Product",
    )

    assert find_product_identity_candidates(
        session,
        brand_id=first_brand.id,
        name="PremiumWhey",
    ) == [first_product]
    assert (
        find_product_identity_candidates(
            session,
            brand_id=second_brand.id,
            name="PremiumWhey",
        )
        == []
    )


@pytest.mark.parametrize(
    "status",
    [ExtractionRunStatus.RUNNING, ExtractionRunStatus.FAILED],
)
def test_only_successful_runs_can_be_resolved(
    session: Session,
    status: ExtractionRunStatus,
) -> None:
    brand, product = make_brand_product(session)
    run = make_run(session, status=status)

    with pytest.raises(UnreviewableIdentityExtractionRunError):
        resolve_extraction_identity(
            session,
            request(
                run,
                brand={"action": "use_existing", "brand_id": brand.id},
                product={"action": "use_existing", "product_id": product.id},
            ),
        )


def test_one_resolution_per_run_cannot_be_rewritten(session: Session) -> None:
    brand, product = make_brand_product(session)
    run = make_run(session)
    original = resolve_extraction_identity(
        session,
        request(
            run,
            brand={"action": "use_existing", "brand_id": brand.id},
            product={"action": "use_existing", "product_id": product.id},
        ),
    )

    with pytest.raises(DuplicateIdentityResolutionError):
        resolve_extraction_identity(
            session,
            request(
                run,
                brand={"action": "create_new", "name": "Replacement Brand"},
                product={"action": "create_new", "name": "Replacement Product"},
            ),
        )

    assert run.identity_resolution is original
    assert session.query(ExtractionIdentityResolution).count() == 1


def test_failed_product_step_can_be_rolled_back_without_orphan_brand(
    identity_store,
) -> None:
    _, factory = identity_store
    with factory() as session:
        _, existing_product = make_brand_product(session)
        run = make_run(session)
        session.commit()
        run_id = run.id
        product_id = existing_product.id

    with factory() as session:
        run = session.get(ExtractionRun, run_id)
        assert run is not None
        with pytest.raises(ProductBrandMismatchError):
            resolve_extraction_identity(
                session,
                request(
                    run,
                    brand={"action": "create_new", "name": "Temporary Brand"},
                    product={"action": "use_existing", "product_id": product_id},
                ),
            )
        assert session.scalar(
            select(Brand).where(Brand.identity_key == "temporary brand")
        ) is not None
        session.rollback()

    with factory() as session:
        assert session.scalar(
            select(Brand).where(Brand.identity_key == "temporary brand")
        ) is None
        assert session.scalar(
            select(func.count()).select_from(ExtractionIdentityResolution)
        ) == 0


@pytest.mark.parametrize(
    "brand_observation, product_observation",
    [
        (
            observation(None, state="not_legible"),
            observation("Observed Product"),
        ),
        (
            observation("Observed Brand"),
            observation(None, state="not_present"),
        ),
    ],
)
def test_human_resolution_does_not_depend_on_model_observation_state(
    session: Session,
    brand_observation: dict,
    product_observation: dict,
) -> None:
    brand, product = make_brand_product(session)
    result = extraction_result(
        brand_name=brand_observation,
        product_name=product_observation,
    )
    run = make_run(session, result=result)
    original_result = dict(run.structured_result)

    resolve_extraction_identity(
        session,
        request(
            run,
            brand={"action": "use_existing", "brand_id": brand.id},
            product={"action": "use_existing", "product_id": product.id},
        ),
    )

    assert run.structured_result == original_result


def test_run_without_sku_resolves_without_creating_sku(session: Session) -> None:
    run = make_run(session)
    before = session.scalar(select(func.count()).select_from(SKU))

    resolution = resolve_extraction_identity(
        session,
        request(
            run,
            brand={"action": "create_new", "name": "Landerfit"},
            product={"action": "create_new", "name": "Premium Whey"},
        ),
    )

    assert run.sku_id is None
    assert session.scalar(select(func.count()).select_from(SKU)) == before
    assert resolution.product.skus == []


def test_run_with_matching_existing_sku_product_is_allowed(session: Session) -> None:
    brand, product = make_brand_product(session)
    sku = SKU(product=product)
    session.add(sku)
    session.flush()
    run = make_run(session, sku=sku)

    resolution = resolve_extraction_identity(
        session,
        request(
            run,
            brand={"action": "use_existing", "brand_id": brand.id},
            product={"action": "use_existing", "product_id": product.id},
        ),
    )

    assert resolution.product_id == sku.product_id
    assert sku.product is product


def test_run_with_conflicting_sku_product_is_rejected_without_reassignment(
    session: Session,
) -> None:
    first_brand, first_product = make_brand_product(session)
    second_brand, second_product = make_brand_product(
        session,
        brand_name="Second Brand",
        product_name="Second Product",
    )
    sku = SKU(product=first_product)
    session.add(sku)
    session.flush()
    original_product_id = sku.product_id
    run = make_run(session, sku=sku)

    with pytest.raises(SKUProductConflictError):
        resolve_extraction_identity(
            session,
            request(
                run,
                brand={"action": "use_existing", "brand_id": second_brand.id},
                product={"action": "use_existing", "product_id": second_product.id},
            ),
        )

    assert sku.product_id == original_product_id
    assert sku.product is first_product
    assert run.identity_resolution is None
    assert first_brand.id != second_brand.id


def test_identity_resolution_contract_is_discriminated_and_strict() -> None:
    base = {
        "extraction_run_id": str(uuid.uuid4()),
        "brand": {"action": "use_existing", "brand_id": str(uuid.uuid4())},
        "product": {"action": "create_new", "name": "Product"},
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExtractionIdentityResolutionRequest.model_validate(
            {**base, "brand": {**base["brand"], "name": "Not allowed"}}
        )
    with pytest.raises(ValidationError):
        ExtractionIdentityResolutionRequest.model_validate(
            {**base, "product": {"action": "create_new"}}
        )
