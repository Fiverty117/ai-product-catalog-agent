import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, Brand, Photo, Product, SKU, SKUFieldProvenance
from app.db.session import create_sqlite_engine
from app.domain.enums import FieldSource, FieldState, PhotoRole, SKUFieldName
from app.domain.schemas import PhotoCreate, SKUCreate
from app.services.photo_ownership import (
    PhotoAlreadyOwnedError,
    UnknownPhotoProductError,
    assign_photo_to_product,
    assign_photo_to_sku,
    get_effective_photos_for_sku,
)
from app.services.sku_variants import (
    DuplicateSKUVariantError,
    UnknownSKUProductError,
    create_manual_sku,
    find_sku_variant_candidates,
)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'variants-media.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_product(session: Session, name: str = "Premium Whey") -> Product:
    product = Product(name=name, brand=Brand(name=f"Brand {uuid.uuid4()}"))
    session.add(product)
    session.flush()
    return product


def make_photo(
    session: Session,
    *,
    sku: SKU | None = None,
    product: Product | None = None,
    role: PhotoRole = PhotoRole.FRONT,
    marker: str = "a",
    created_at: datetime | None = None,
) -> Photo:
    photo = Photo(
        sku=sku,
        product=product,
        file_path=f"storage/originals/{marker}.jpg",
        checksum_sha256=marker * 64,
        original_filename=f"{marker}.jpg",
        mime_type="image/jpeg",
        file_size_bytes=123,
        width=10,
        height=20,
        role=role,
        is_original=True,
        created_at=created_at or datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    session.add(photo)
    session.flush()
    return photo


@pytest.mark.parametrize(
    "fields",
    [
        {"flavor": "Vanilla"},
        {"size_value": Decimal("250"), "size_unit": "g"},
        {
            "flavor": "Chocolate",
            "size_value": Decimal("2"),
            "size_unit": "lb",
        },
        {"servings": 30},
        {},
    ],
)
def test_manual_sku_creation_supports_each_variant_shape(
    session: Session, fields: dict
) -> None:
    product = make_product(session)

    sku = create_manual_sku(session, {"product_id": product.id, **fields})

    assert sku.product is product
    assert sku.flavor == fields.get("flavor")
    assert sku.size_value == fields.get("size_value")
    assert sku.size_unit == fields.get("size_unit")
    assert sku.servings == fields.get("servings")
    assert sku.photos == []


@pytest.mark.parametrize(
    "fields",
    [
        {"size_value": Decimal("1")},
        {"size_unit": "kg"},
        {"size_value": Decimal("0"), "size_unit": "kg"},
    ],
)
def test_manual_sku_rejects_invalid_size_pair(
    session: Session, fields: dict
) -> None:
    product = make_product(session)

    with pytest.raises(ValidationError):
        create_manual_sku(session, {"product_id": product.id, **fields})


def test_manual_values_are_decimal_and_human_verified_locked(
    session: Session,
) -> None:
    product = make_product(session)

    sku = create_manual_sku(
        session,
        SKUCreate(
            product_id=product.id,
            external_sku="PW-VAN-2LB",
            flavor="Vanilla",
            size_value=Decimal("2.125000"),
            size_unit="lb",
            servings=29,
        ),
    )

    assert isinstance(sku.size_value, Decimal)
    assert sku.size_value == Decimal("2.125000")
    provenance = {record.field_name: record for record in sku.field_provenance}
    assert set(provenance) == set(SKUFieldName)
    assert all(record.source is FieldSource.HUMAN for record in provenance.values())
    assert all(record.state is FieldState.VERIFIED for record in provenance.values())
    assert all(record.locked is True for record in provenance.values())
    assert all(record.confidence is None for record in provenance.values())
    assert all(record.extraction_run_id is None for record in provenance.values())
    assert {
        SKUFieldName.SIZE_VALUE,
        SKUFieldName.SIZE_UNIT,
    } <= set(provenance)


def test_manual_sku_requires_existing_product(session: Session) -> None:
    with pytest.raises(UnknownSKUProductError):
        create_manual_sku(
            session,
            {"product_id": uuid.uuid4(), "flavor": "Vanilla"},
        )


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        ("250", "g"),
        ("1", "kg"),
        ("330", "ml"),
        ("1", "L"),
        ("2", "lb"),
        ("12", "future-unit-with words"),
    ],
)
def test_open_size_units_are_stored_without_conversion(
    session: Session, value: str, unit: str
) -> None:
    product = make_product(session)

    sku = create_manual_sku(
        session,
        {
            "product_id": product.id,
            "size_value": Decimal(value),
            "size_unit": unit,
        },
    )

    assert sku.size_value == Decimal(value)
    assert sku.size_unit == unit


def test_units_are_not_converted_for_duplicate_comparison(session: Session) -> None:
    product = make_product(session)
    first = create_manual_sku(
        session,
        {"product_id": product.id, "size_value": Decimal("1000"), "size_unit": "g"},
    )

    second = create_manual_sku(
        session,
        {"product_id": product.id, "size_value": Decimal("1"), "size_unit": "kg"},
    )

    assert first.id != second.id


def test_exact_variant_comparison_normalizes_text_only(session: Session) -> None:
    product = make_product(session)
    existing = create_manual_sku(
        session,
        {
            "product_id": product.id,
            "flavor": "Vanilla",
            "size_value": Decimal("2.0"),
            "size_unit": "lb",
        },
    )

    candidates = find_sku_variant_candidates(
        session,
        {
            "product_id": product.id,
            "flavor": "  ＶＡＮＩＬＬＡ ",
            "size_value": Decimal("2.000000"),
            "size_unit": "LB",
        },
    )

    assert candidates.exact == (existing,)


def test_exact_duplicate_rejected_and_partial_variant_only_suggested(
    session: Session,
) -> None:
    product = make_product(session)
    existing = create_manual_sku(
        session, {"product_id": product.id, "flavor": "Vanilla"}
    )

    partial = find_sku_variant_candidates(
        session,
        {
            "product_id": product.id,
            "flavor": "vanilla",
            "size_value": Decimal("2"),
            "size_unit": "lb",
        },
    )
    created = create_manual_sku(
        session,
        {
            "product_id": product.id,
            "flavor": "vanilla",
            "size_value": Decimal("2"),
            "size_unit": "lb",
        },
    )

    assert partial.exact == ()
    assert partial.partial == (existing,)
    assert created.id != existing.id
    with pytest.raises(DuplicateSKUVariantError, match="use existing"):
        create_manual_sku(
            session,
            {
                "product_id": product.id,
                "flavor": "VANILLA",
                "size_value": Decimal("2.000000"),
                "size_unit": "LB",
            },
        )


def test_same_variant_is_allowed_under_different_product(session: Session) -> None:
    first_product = make_product(session, "First")
    second_product = make_product(session, "Second")

    first = create_manual_sku(
        session, {"product_id": first_product.id, "flavor": "Vanilla"}
    )
    second = create_manual_sku(
        session, {"product_id": second_product.id, "flavor": "Vanilla"}
    )

    assert first.id != second.id


def test_dimensionless_duplicate_is_rejected(session: Session) -> None:
    product = make_product(session)
    existing = create_manual_sku(session, {"product_id": product.id})

    with pytest.raises(DuplicateSKUVariantError) as error:
        create_manual_sku(session, {"product_id": product.id})

    assert error.value.existing_sku is existing


def test_external_sku_is_candidate_not_identity(session: Session) -> None:
    first_product = make_product(session, "First")
    second_product = make_product(session, "Second")
    existing = create_manual_sku(
        session,
        {
            "product_id": first_product.id,
            "external_sku": "SHARED-001",
            "flavor": "Vanilla",
        },
    )

    candidates = find_sku_variant_candidates(
        session,
        {
            "product_id": first_product.id,
            "external_sku": "SHARED-001",
            "flavor": "Chocolate",
        },
    )
    same_product = create_manual_sku(
        session,
        {
            "product_id": first_product.id,
            "external_sku": "SHARED-001",
            "flavor": "Chocolate",
        },
    )
    other_product = create_manual_sku(
        session,
        {
            "product_id": second_product.id,
            "external_sku": "SHARED-001",
            "flavor": "Chocolate",
        },
    )

    assert candidates.external_sku == (existing,)
    assert candidates.exact == ()
    assert len({existing.id, same_product.id, other_product.id}) == 3


def test_failed_provenance_can_roll_back_entire_manual_creation(
    session: Session, monkeypatch
) -> None:
    product = make_product(session)
    session.commit()
    original_apply = __import__(
        "app.services.sku_variants", fromlist=["apply_sku_field_update"]
    ).apply_sku_field_update
    calls = 0

    def fail_second_update(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("provenance write failed")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.sku_variants.apply_sku_field_update", fail_second_update
    )

    with pytest.raises(RuntimeError, match="provenance write failed"):
        create_manual_sku(
            session,
            {
                "product_id": product.id,
                "size_value": Decimal("2"),
                "size_unit": "lb",
            },
        )
    session.rollback()

    assert session.scalar(select(func.count()).select_from(SKU)) == 0
    assert session.scalar(select(func.count()).select_from(SKUFieldProvenance)) == 0


def test_photo_ownership_assignment_is_explicit_and_metadata_only(
    session: Session,
) -> None:
    product = make_product(session)
    sku = create_manual_sku(session, {"product_id": product.id})
    photo = make_photo(session)
    metadata = (
        photo.file_path,
        photo.checksum_sha256,
        photo.original_filename,
        photo.mime_type,
        photo.file_size_bytes,
        photo.width,
        photo.height,
        photo.role,
        photo.is_original,
    )

    assigned = assign_photo_to_product(
        session, photo_id=photo.id, product_id=product.id
    )
    same = assign_photo_to_product(
        session, photo_id=photo.id, product_id=product.id
    )

    assert assigned is same is photo
    assert photo.product is product
    assert photo.product_id == product.id
    assert photo.sku_id is None
    with pytest.raises(PhotoAlreadyOwnedError):
        assign_photo_to_sku(session, photo_id=photo.id, sku_id=sku.id)
    assert photo.product_id == product.id
    assert photo.sku_id is None

    assign_photo_to_sku(
        session, photo_id=photo.id, sku_id=sku.id, replace_owner=True
    )
    assert photo.product_id is None
    assert photo.sku is sku
    assert (
        photo.file_path,
        photo.checksum_sha256,
        photo.original_filename,
        photo.mime_type,
        photo.file_size_bytes,
        photo.width,
        photo.height,
        photo.role,
        photo.is_original,
    ) == metadata


def test_unassigned_photo_can_be_assigned_directly_to_sku(session: Session) -> None:
    product = make_product(session)
    sku = create_manual_sku(session, {"product_id": product.id})
    photo = make_photo(session)

    assign_photo_to_sku(session, photo_id=photo.id, sku_id=sku.id)

    assert photo.sku is sku
    assert photo.product_id is None


def test_photo_schema_and_database_reject_simultaneous_ownership(
    session: Session,
) -> None:
    product = make_product(session)
    sku = create_manual_sku(session, {"product_id": product.id})

    with pytest.raises(ValidationError, match="both a Product and an SKU"):
        PhotoCreate(
            sku_id=sku.id,
            product_id=product.id,
            file_path="storage/originals/both.jpg",
            checksum_sha256="a" * 64,
            original_filename="both.jpg",
            mime_type="image/jpeg",
            file_size_bytes=10,
            width=10,
            height=10,
            is_original=True,
        )

    session.add(
        Photo(
            sku=sku,
            product=product,
            file_path="storage/originals/both.jpg",
            checksum_sha256="b" * 64,
            original_filename="both.jpg",
            mime_type="image/jpeg",
            file_size_bytes=10,
            width=10,
            height=10,
            role=PhotoRole.FRONT,
            is_original=True,
        )
    )
    with pytest.raises(IntegrityError, match="ck_photos_single_owner"):
        session.flush()


def test_failed_photo_reassignment_leaves_owner_unchanged(session: Session) -> None:
    product = make_product(session)
    photo = make_photo(session, product=product)

    with pytest.raises(UnknownPhotoProductError):
        assign_photo_to_product(
            session,
            photo_id=photo.id,
            product_id=uuid.uuid4(),
            replace_owner=True,
        )

    assert photo.product_id == product.id
    assert photo.sku_id is None


def test_effective_media_prefers_sku_level_with_deterministic_order(
    session: Session,
) -> None:
    product = make_product(session)
    sku = create_manual_sku(session, {"product_id": product.id})
    shared = make_photo(session, product=product, marker="a")
    later = make_photo(
        session,
        sku=sku,
        marker="c",
        created_at=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
    )
    earlier = make_photo(
        session,
        sku=sku,
        marker="b",
        created_at=datetime(2026, 9, 15, 11, 0, tzinfo=timezone.utc),
    )

    effective = get_effective_photos_for_sku(session, sku.id)

    assert effective == [earlier, later]
    assert shared not in effective


def test_effective_media_falls_back_by_same_role_without_sibling_leak(
    session: Session,
) -> None:
    product = make_product(session)
    target = create_manual_sku(
        session, {"product_id": product.id, "flavor": "Chocolate"}
    )
    sibling = create_manual_sku(
        session, {"product_id": product.id, "flavor": "Vanilla"}
    )
    product_front = make_photo(session, product=product, marker="a")
    product_back = make_photo(
        session, product=product, role=PhotoRole.BACK, marker="b"
    )
    sibling_front = make_photo(session, sku=sibling, marker="c")

    front = get_effective_photos_for_sku(session, target.id, PhotoRole.FRONT)
    nutrition = get_effective_photos_for_sku(
        session, target.id, PhotoRole.NUTRITION
    )

    assert front == [product_front]
    assert product_back not in front
    assert sibling_front not in front
    assert nutrition == []


def test_sku_without_any_photo_is_valid(session: Session) -> None:
    product = make_product(session)
    sku = create_manual_sku(session, {"product_id": product.id})

    assert get_effective_photos_for_sku(session, sku.id) == []
