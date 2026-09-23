import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Brand,
    CatalogSnapshot,
    Category,
    DerivedImage,
    Photo,
    Price,
    Product,
    ProductCategory,
    ProductCopyRun,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import (
    DerivedImageReviewDecision,
    ProductCopyReviewDecision,
    PhotoPresentationAssetType,
    PhotoRole,
)
from app.domain.schemas import (
    CatalogSnapshotCreate,
    DerivedImageReviewCreate,
    ImageEnhancementJobPayload,
    ProductCopyReviewRequest,
)
from app.services.catalog_snapshots import (
    CatalogSnapshotAssetIntegrityError,
    CatalogSnapshotIntegrityError,
    CatalogSnapshotReadinessError,
    CatalogSnapshotSelectionError,
    canonical_catalog_snapshot_json,
    create_catalog_snapshot,
    read_catalog_snapshot_data,
)
from app.services.categories import assign_product_category, create_category
from app.services.image_enhancement import (
    complete_image_enhancement_run,
    create_running_image_enhancement_run,
    store_processed_image,
)
from app.services.image_presentation import (
    create_derived_image_review,
    select_derived_image_for_photo,
    use_original_photo_presentation,
)
from app.services.photo_intake import register_original_photo
from app.services.product_copy import (
    build_product_copy_input_snapshot,
    build_product_copy_source_fingerprint,
    create_running_product_copy_run,
    mark_product_copy_run_succeeded,
)
from app.services.product_copy_review import apply_product_copy_review
from app.domain.schemas import ProductCopyJobPayload


AS_OF = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'snapshots.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.info["storage_root"] = tmp_path / "storage"
        yield session
    engine.dispose()


def image_bytes(color=(10, 20, 30), image_format="PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (13, 11), color).save(output, format=image_format)
    return output.getvalue()


def make_ready_product(
    session: Session,
    *,
    product_name: str = "Catalog Product",
    brand_name: str | None = None,
    category: Category | None = None,
    sku_values: list[dict] | None = None,
) -> tuple[Product, Category, list[SKU], list[Price], Photo]:
    suffix = uuid.uuid4().hex
    product = Product(
        name=product_name,
        brand=Brand(name=brand_name or f"Brand {suffix}"),
    )
    session.add(product)
    session.flush()
    if category is None:
        category = create_category(session, name=f"Category {suffix}")
    assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    skus: list[SKU] = []
    prices: list[Price] = []
    for values in sku_values or [{}]:
        priced = values.pop("priced", True)
        sku = SKU(product=product, **values)
        session.add(sku)
        session.flush()
        skus.append(sku)
        if priced:
            price = Price(
                sku=sku,
                amount=Decimal("180000.1200"),
                currency="PYG",
                valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source="manual",
                approved=True,
            )
            session.add(price)
            prices.append(price)
    photo = register_original_photo(
        session,
        image_bytes=image_bytes(),
        original_filename="front.png",
        originals_dir=session.info["storage_root"] / "originals",
        role=PhotoRole.FRONT,
    )
    photo.product = product
    session.flush()
    return product, category, skus, prices, photo


def make_derived(session: Session, photo: Photo) -> DerivedImage:
    payload = ImageEnhancementJobPayload(
        source_photo_id=photo.id,
        source_checksum_sha256=photo.checksum_sha256,
        provider="openai",
        model="gpt-image-2.5-sunburst",
        prompt_version="product-image-enhancement-v1",
        config_version="image-enhancement-config-v1",
        parameters={
            "quality": "high",
            "output_format": "png",
            "size": "auto",
            "background": "auto",
        },
    )
    run = create_running_image_enhancement_run(session, payload=payload)
    stored = store_processed_image(
        image_bytes((80, 90, 100)),
        processed_dir=session.info["storage_root"] / "processed",
    )
    return complete_image_enhancement_run(session, run, stored_image=stored)


def approve_and_select(session: Session, photo: Photo) -> DerivedImage:
    derived = make_derived(session, photo)
    create_derived_image_review(
        session,
        DerivedImageReviewCreate(
            derived_image_id=derived.id,
            decision=DerivedImageReviewDecision.APPROVED,
        ),
    )
    select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )
    return derived


def create_snapshot(
    session: Session,
    product_ids: list[uuid.UUID],
    **request_values,
) -> CatalogSnapshot:
    return create_catalog_snapshot(
        session,
        CatalogSnapshotCreate(
            product_ids=product_ids,
            currency="PYG",
            as_of=AS_OF,
            **request_values,
        ),
        storage_root=session.info["storage_root"],
    )


def make_copy_run(
    session: Session,
    product: Product,
    text: str,
) -> ProductCopyRun:
    input_snapshot = build_product_copy_input_snapshot(session, product.id)
    payload = ProductCopyJobPayload(
        product_id=product.id,
        copy_type="short_description",
        provider="openai",
        model="gpt-5.6-sol",
        prompt_version="product-copy-v1",
        schema_version="product-copy-result-v1",
        parameters={"reasoning_effort": "low"},
        source_fingerprint=build_product_copy_source_fingerprint(input_snapshot),
        input_snapshot=input_snapshot,
    )
    run = create_running_product_copy_run(session, payload=payload, started_at=AS_OF)
    return mark_product_copy_run_succeeded(
        session,
        run,
        structured_result={"short_description": text},
        completed_at=AS_OF,
    )


def review_copy(
    session: Session,
    run: ProductCopyRun,
    decision: ProductCopyReviewDecision,
    corrected: str | None = None,
):
    return apply_product_copy_review(
        session,
        ProductCopyReviewRequest(
            product_copy_run_id=run.id,
            decision=decision,
            corrected_short_description=corrected,
        ),
        applied_at=AS_OF,
    )


def test_snapshot_request_is_strict_and_requires_valid_explicit_selection() -> None:
    product_id = uuid.uuid4()
    sku_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        CatalogSnapshotCreate.model_validate({"product_ids": [], "currency": "PYG"})
    with pytest.raises(ValidationError):
        CatalogSnapshotCreate.model_validate(
            {"product_ids": [product_id, product_id], "currency": "PYG"}
        )
    with pytest.raises(ValidationError):
        CatalogSnapshotCreate.model_validate({"product_ids": [product_id]})
    with pytest.raises(ValidationError):
        CatalogSnapshotCreate(
            product_ids=[product_id],
            currency="PYG",
            as_of=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        CatalogSnapshotCreate(
            product_ids=[product_id],
            currency="PYG",
            sku_selection={uuid.uuid4(): [sku_id]},
        )
    with pytest.raises(ValidationError):
        CatalogSnapshotCreate(
            product_ids=[product_id],
            currency="PYG",
            sku_selection={product_id: []},
        )
    with pytest.raises(ValidationError):
        CatalogSnapshotCreate(
            product_ids=[product_id],
            currency="PYG",
            sku_selection={product_id: [sku_id, sku_id]},
        )


def test_default_policy_freezes_publishable_values_and_one_resolved_as_of(
    session: Session,
) -> None:
    product, category, skus, prices, photo = make_ready_product(
        session,
        sku_values=[
            {
                "external_sku": "SKU-1",
                "flavor": "Vanilla",
                "size_value": Decimal("500.125000"),
                "size_unit": "g",
                "servings": 20,
            },
            {"flavor": "Unpriced", "priced": False},
        ],
    )
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return AS_OF

    snapshot = create_catalog_snapshot(
        session,
        {"product_ids": [product.id], "currency": "pyg"},
        storage_root=session.info["storage_root"],
        clock=clock,
    )
    data = read_catalog_snapshot_data(session, snapshot.id)
    frozen_product = data.sections[0].products[0]
    variant = frozen_product.variants[0]

    assert calls == 1
    assert data.as_of == AS_OF
    assert data.currency == "PYG"
    assert data.sections[0].category.source_category_id == category.id
    assert [item.source_sku_id for item in frozen_product.variants] == [skus[0].id]
    assert variant.price.source_price_id == prices[0].id
    assert variant.price.amount == Decimal("180000.1200")
    assert variant.size_value == Decimal("500.125000")
    assert snapshot.payload["sections"][0]["products"][0]["variants"][0][
        "price"
    ]["amount"] == "180000.12"
    assert snapshot.payload["sections"][0]["products"][0]["variants"][0][
        "size_value"
    ] == "500.125"
    asset = frozen_product.hero.source_original_asset
    assert asset.checksum_sha256 == photo.checksum_sha256
    assert asset.storage_relative_path.startswith("originals/")
    assert not Path(asset.storage_relative_path).is_absolute()
    assert str(session.info["storage_root"]) not in json.dumps(snapshot.payload)


def test_explicit_sku_subset_is_exact_and_invalid_selection_is_atomic(
    session: Session,
) -> None:
    product, _, skus, _, _ = make_ready_product(
        session,
        sku_values=[{"flavor": "B"}, {"flavor": "A"}, {"priced": False}],
    )
    other, _, other_skus, _, _ = make_ready_product(session)
    snapshot = create_snapshot(
        session,
        [product.id],
        sku_selection={product.id: [skus[0].id]},
    )
    assert [
        item["source_sku_id"]
        for item in snapshot.payload["sections"][0]["products"][0]["variants"]
    ] == [str(skus[0].id)]

    before = session.scalar(select(func.count()).select_from(CatalogSnapshot))
    with pytest.raises(CatalogSnapshotSelectionError, match="not publishable"):
        create_snapshot(
            session,
            [product.id],
            sku_selection={product.id: [skus[2].id]},
        )
    with pytest.raises(CatalogSnapshotSelectionError, match="does not belong"):
        create_snapshot(
            session,
            [product.id],
            sku_selection={product.id: [other_skus[0].id]},
        )
    assert session.scalar(select(func.count()).select_from(CatalogSnapshot)) == before
    assert other.id != product.id


def test_one_nonready_product_rejects_all_while_warnings_do_not_block(
    session: Session,
) -> None:
    ready, *_ = make_ready_product(
        session, sku_values=[{}, {"flavor": "warning-only", "priced": False}]
    )
    not_ready = Product(name="Not Ready", brand=Brand(name="Not Ready Brand"))
    session.add(not_ready)
    session.flush()

    with pytest.raises(CatalogSnapshotReadinessError) as exc_info:
        create_snapshot(session, [ready.id, not_ready.id])
    assert exc_info.value.failures[0].product_id == not_ready.id
    assert session.scalar(select(func.count()).select_from(CatalogSnapshot)) == 0

    snapshot = create_snapshot(session, [ready.id])
    assert len(snapshot.payload["sections"][0]["products"][0]["variants"]) == 1


def test_grouping_and_all_ordering_are_deterministic(session: Session) -> None:
    later = create_category(session, name="Later", sort_order=20)
    shared = create_category(session, name="Shared", sort_order=10)
    product_z, *_ = make_ready_product(
        session,
        product_name="Zulu",
        brand_name="Zulu Brand",
        category=shared,
        sku_values=[
            {"flavor": "vanilla", "size_value": "2", "size_unit": "kg"},
            {"flavor": "Chocolate", "size_value": "1", "size_unit": "kg"},
        ],
    )
    product_a, *_ = make_ready_product(
        session,
        product_name="Alpha",
        brand_name="Alpha Brand",
        category=shared,
    )
    product_later, *_ = make_ready_product(
        session,
        product_name="First Input",
        brand_name="First Brand",
        category=later,
    )
    secondary = create_category(session, name="Secondary", sort_order=0)
    assign_product_category(
        session, product_id=product_a.id, category_id=secondary.id
    )

    first = create_snapshot(
        session, [product_later.id, product_z.id, product_a.id]
    )
    second = create_snapshot(
        session, [product_a.id, product_z.id, product_later.id]
    )

    assert first.id != second.id
    assert first.content_hash == second.content_hash
    assert first.payload == second.payload
    assert [item["category"]["name"] for item in first.payload["sections"]] == [
        "Shared",
        "Later",
    ]
    assert [
        item["product_name"] for item in first.payload["sections"][0]["products"]
    ] == ["Alpha", "Zulu"]
    assert [
        item["flavor"]
        for item in first.payload["sections"][0]["products"][1]["variants"]
    ] == ["Chocolate", "vanilla"]
    assert len(first.payload["sections"]) == 2
    assert canonical_catalog_snapshot_json(read_catalog_snapshot_data(session, first.id)) == (
        canonical_catalog_snapshot_json(read_catalog_snapshot_data(session, second.id))
    )


def test_live_changes_and_later_image_rejection_do_not_change_snapshot(
    session: Session,
) -> None:
    product, category, skus, _, photo = make_ready_product(
        session,
        product_name="Frozen Product",
        brand_name="Frozen Brand",
        sku_values=[
            {
                "external_sku": "OLD",
                "flavor": "Vanilla",
                "size_value": "250.500000",
                "size_unit": "g",
                "servings": 15,
            }
        ],
    )
    derived = approve_and_select(session, photo)
    snapshot = create_snapshot(session, [product.id])
    frozen_payload = json.loads(json.dumps(snapshot.payload))
    frozen_hash = snapshot.content_hash

    product.brand.name = "Changed Brand"
    product.name = "Changed Product"
    category.name = "Changed Category"
    category.sort_order = 1
    skus[0].external_sku = "NEW"
    skus[0].flavor = "Chocolate"
    skus[0].size_value = Decimal("1")
    skus[0].size_unit = "kg"
    skus[0].servings = 99
    session.add(
        Price(
            sku=skus[0],
            amount=Decimal("190000.0000"),
            currency="PYG",
            valid_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
            source="manual",
            approved=True,
        )
    )
    use_original_photo_presentation(session, photo_id=photo.id)
    create_derived_image_review(
        session,
        DerivedImageReviewCreate(
            derived_image_id=derived.id,
            decision=DerivedImageReviewDecision.REJECTED,
        ),
    )
    session.flush()

    data = read_catalog_snapshot_data(session, snapshot.id)
    assert snapshot.payload == frozen_payload
    assert snapshot.content_hash == frozen_hash
    assert data.sections[0].category.name != category.name
    assert data.sections[0].products[0].brand_name != product.brand.name
    assert data.sections[0].products[0].product_name != product.name
    assert data.sections[0].products[0].variants[0].flavor == "Vanilla"
    assert data.sections[0].products[0].variants[0].price.amount == Decimal(
        "180000.12"
    )
    assert data.sections[0].products[0].hero.presentation_type is (
        PhotoPresentationAssetType.DERIVED
    )
    assert data.sections[0].products[0].hero.source_derived_image_id == derived.id


def test_presentation_freezes_only_explicit_selected_derived_asset(
    session: Session,
) -> None:
    product, _, _, _, photo = make_ready_product(session)
    derived = make_derived(session, photo)
    create_derived_image_review(
        session,
        DerivedImageReviewCreate(
            derived_image_id=derived.id,
            decision=DerivedImageReviewDecision.APPROVED,
        ),
    )
    original = create_snapshot(session, [product.id])
    original_hero = original.payload["sections"][0]["products"][0]["hero"]
    assert original_hero["presentation_type"] == "original"
    assert original_hero["source_derived_image_id"] is None

    select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )
    selected = create_snapshot(session, [product.id])
    hero = selected.payload["sections"][0]["products"][0]["hero"]
    assert hero["presentation_type"] == "derived"
    assert hero["source_derived_image_id"] == str(derived.id)
    assert hero["source_original_asset"]["checksum_sha256"] == photo.checksum_sha256
    assert hero["presentation_asset"]["checksum_sha256"] == derived.checksum_sha256
    assert hero["presentation_asset"]["storage_relative_path"].startswith(
        "processed/"
    )


@pytest.mark.parametrize("failure", ["missing", "checksum", "corrupt"])
def test_selected_derived_asset_failure_rejects_snapshot_atomically(
    session: Session, failure: str
) -> None:
    product, _, _, _, photo = make_ready_product(session)
    derived = approve_and_select(session, photo)
    path = Path(derived.file_path)
    if failure == "missing":
        path.unlink()
    elif failure == "checksum":
        path.write_bytes(image_bytes((2, 3, 4)))
    else:
        content = b"corrupt"
        path.write_bytes(content)
        derived.checksum_sha256 = hashlib.sha256(content).hexdigest()
        derived.file_size_bytes = len(content)
        session.flush()

    with pytest.raises(CatalogSnapshotAssetIntegrityError):
        create_snapshot(session, [product.id])
    assert session.scalar(select(func.count()).select_from(CatalogSnapshot)) == 0


@pytest.mark.parametrize("failure", ["checksum", "corrupt", "outside", "traversal"])
def test_original_asset_integrity_and_canonical_path_are_enforced(
    session: Session, failure: str
) -> None:
    product, _, _, _, photo = make_ready_product(session)
    path = Path(photo.file_path)
    if failure == "checksum":
        path.write_bytes(image_bytes((2, 3, 4)))
    elif failure == "corrupt":
        content = b"corrupt"
        path.write_bytes(content)
        photo.checksum_sha256 = hashlib.sha256(content).hexdigest()
        photo.file_size_bytes = len(content)
    elif failure == "outside":
        outside = session.info["storage_root"].parent / "outside.png"
        outside.write_bytes(path.read_bytes())
        photo.file_path = str(outside)
    else:
        prefix = path.parent.parent
        detour = prefix / "detour"
        detour.mkdir()
        photo.file_path = str(detour / ".." / path.parent.name / path.name)
    session.flush()

    with pytest.raises(CatalogSnapshotAssetIntegrityError):
        create_snapshot(session, [product.id])
    assert session.scalar(select(func.count()).select_from(CatalogSnapshot)) == 0


def test_missing_original_asset_rejects_snapshot_without_row(session: Session) -> None:
    product, _, _, _, photo = make_ready_product(session)
    Path(photo.file_path).unlink()

    with pytest.raises(CatalogSnapshotReadinessError):
        create_snapshot(session, [product.id])
    assert session.scalar(select(func.count()).select_from(CatalogSnapshot)) == 0


def test_read_detects_tampered_payload_or_hash_without_live_repair(
    session: Session,
) -> None:
    product, *_ = make_ready_product(session)
    snapshot = create_snapshot(session, [product.id])
    original_payload = json.loads(json.dumps(snapshot.payload))
    original_hash = snapshot.content_hash

    tampered = json.loads(json.dumps(snapshot.payload))
    tampered["sections"][0]["products"][0]["product_name"] = "Tampered"
    snapshot.payload = tampered
    session.flush()
    with pytest.raises(CatalogSnapshotIntegrityError, match="content-hash"):
        read_catalog_snapshot_data(session, snapshot.id)

    snapshot.payload = original_payload
    snapshot.content_hash = "0" * 64
    session.flush()
    with pytest.raises(CatalogSnapshotIntegrityError, match="content-hash"):
        read_catalog_snapshot_data(session, snapshot.id)
    snapshot.content_hash = original_hash


def test_changed_frozen_content_changes_hash_and_caller_owns_transaction(
    session: Session,
) -> None:
    product, _, skus, _, _ = make_ready_product(session)
    session.commit()
    first = create_snapshot(session, [product.id])
    first_hash = first.content_hash
    session.rollback()
    assert session.get(CatalogSnapshot, first.id) is None

    first = create_snapshot(session, [product.id])
    session.commit()
    skus[0].flavor = "New Flavor"
    session.flush()
    second = create_snapshot(session, [product.id])
    assert second.content_hash != first_hash
    assert session.scalar(select(func.count()).select_from(CatalogSnapshot)) == 2


def test_snapshot_creation_does_not_mutate_canonical_rows(session: Session) -> None:
    product, category, skus, prices, photo = make_ready_product(session)
    before = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in (Brand, Product, SKU, Price, Category, ProductCategory, Photo)
    }
    canonical_values = (
        product.name,
        product.updated_at,
        category.name,
        skus[0].updated_at,
        prices[0].amount,
        photo.file_path,
    )

    create_snapshot(session, [product.id])

    after = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in before
    }
    assert after == before
    assert canonical_values == (
        product.name,
        product.updated_at,
        category.name,
        skus[0].updated_at,
        prices[0].amount,
        photo.file_path,
    )


def test_snapshot_includes_only_current_human_reviewed_product_copy(
    session: Session,
) -> None:
    approved_product, *_ = make_ready_product(session, product_name="Approved")
    corrected_product, *_ = make_ready_product(session, product_name="Corrected")
    pending_product, *_ = make_ready_product(session, product_name="Pending")
    rejected_product, *_ = make_ready_product(session, product_name="Rejected")
    stale_product, *_ = make_ready_product(session, product_name="Stale")
    no_copy_product, *_ = make_ready_product(session, product_name="No Copy")

    review_copy(
        session,
        make_copy_run(session, approved_product, "Descripcion aprobada vigente."),
        ProductCopyReviewDecision.APPROVED,
    )
    review_copy(
        session,
        make_copy_run(session, corrected_product, "Propuesta original."),
        ProductCopyReviewDecision.CORRECTED,
        "Descripcion corregida por una persona.",
    )
    make_copy_run(session, pending_product, "Propuesta aun no revisada.")
    review_copy(
        session,
        make_copy_run(session, rejected_product, "Propuesta rechazada."),
        ProductCopyReviewDecision.REJECTED,
    )
    review_copy(
        session,
        make_copy_run(session, stale_product, "Descripcion aprobada antigua."),
        ProductCopyReviewDecision.APPROVED,
    )
    stale_product.name = "Stale renamed after review"
    session.flush()

    snapshot = create_snapshot(
        session,
        [
            approved_product.id,
            corrected_product.id,
            pending_product.id,
            rejected_product.id,
            stale_product.id,
            no_copy_product.id,
        ],
    )
    descriptions = {
        product["product_name"]: product.get("short_description")
        for section in snapshot.payload["sections"]
        for product in section["products"]
    }

    assert descriptions["Approved"] == "Descripcion aprobada vigente."
    assert descriptions["Corrected"] == "Descripcion corregida por una persona."
    assert descriptions["Pending"] is None
    assert descriptions["Rejected"] is None
    assert descriptions["Stale renamed after review"] is None
    assert descriptions["No Copy"] is None


def test_pre_block9_snapshot_payload_without_description_remains_readable(
    session: Session,
) -> None:
    product, *_ = make_ready_product(session)
    snapshot = create_snapshot(session, [product.id])
    old_style_payload = json.loads(json.dumps(snapshot.payload))
    old_style_payload["sections"][0]["products"][0].pop("short_description")
    snapshot.payload = old_style_payload
    session.flush()

    data = read_catalog_snapshot_data(session, snapshot.id)

    assert data.sections[0].products[0].short_description is None
    assert snapshot.content_hash == hashlib.sha256(
        canonical_catalog_snapshot_json(data).encode("utf-8")
    ).hexdigest()


def test_reviewed_copy_change_changes_new_snapshot_hash(session: Session) -> None:
    product, *_ = make_ready_product(session)
    first_run = make_copy_run(session, product, "Primera descripcion aprobada.")
    first_review = review_copy(
        session, first_run, ProductCopyReviewDecision.APPROVED
    )
    first_review.applied_at = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    session.flush()
    first_snapshot = create_snapshot(session, [product.id])

    second_run = make_copy_run(session, product, "Segunda descripcion corregida.")
    second_review = review_copy(
        session,
        second_run,
        ProductCopyReviewDecision.CORRECTED,
        "Segunda descripcion humana.",
    )
    second_review.applied_at = datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc)
    session.flush()
    second_snapshot = create_snapshot(session, [product.id])

    assert first_snapshot.content_hash != second_snapshot.content_hash
    assert second_snapshot.payload["sections"][0]["products"][0][
        "short_description"
    ] == "Segunda descripcion humana."
