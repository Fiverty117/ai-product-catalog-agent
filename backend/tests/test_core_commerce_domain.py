import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, Brand, Photo, PhotoRole, Price, Product, SKU
from app.db.session import create_sqlite_engine
from app.domain.schemas import BrandCreate, BrandRead, PhotoCreate, PriceCreate, SKUCreate


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_sku(session: Session) -> SKU:
    brand = Brand(name="Optimum Nutrition")
    product = Product(name="Gold Standard 100% Whey", brand=brand)
    sku = SKU(
        product=product,
        external_sku="ON-GS-VAN-2LB",
        flavor="Vanilla",
        size_value=Decimal("2.000000"),
        size_unit="lb",
        servings=29,
    )
    session.add(sku)
    session.flush()
    return sku


def test_entity_creation_relationships_and_read_schema(session: Session) -> None:
    sku = make_sku(session)
    session.commit()

    assert isinstance(sku.id, uuid.UUID)
    assert sku.product.brand.name == "Optimum Nutrition"
    assert sku in sku.product.skus
    assert sku.product in sku.product.brand.products
    assert sku.created_at.utcoffset() == timedelta(0)
    assert sku.updated_at.utcoffset() == timedelta(0)

    brand_read = BrandRead.model_validate(sku.product.brand)
    assert brand_read == BrandRead(
        id=sku.product.brand.id,
        name="Optimum Nutrition",
        created_at=sku.product.brand.created_at,
        updated_at=sku.product.brand.updated_at,
    )


def test_multiple_photos_per_sku(session: Session) -> None:
    sku = make_sku(session)
    front = Photo(
        sku=sku,
        file_path="storage/originals/front.jpg",
        checksum_sha256="a" * 64,
        original_filename="front.jpg",
        mime_type="image/jpeg",
        file_size_bytes=100,
        width=10,
        height=10,
        role=PhotoRole.FRONT,
        is_original=True,
    )
    nutrition = Photo(
        sku=sku,
        file_path="storage/originals/nutrition.jpg",
        checksum_sha256="b" * 64,
        original_filename="nutrition.jpg",
        mime_type="image/jpeg",
        file_size_bytes=100,
        width=10,
        height=10,
        role=PhotoRole.NUTRITION,
        is_original=True,
    )
    session.add_all([front, nutrition])
    session.commit()

    assert {photo.role for photo in sku.photos} == {
        PhotoRole.FRONT,
        PhotoRole.NUTRITION,
    }
    assert all(photo.sku is sku for photo in sku.photos)


def test_multiple_historical_prices_preserve_decimal_precision(session: Session) -> None:
    sku = make_sku(session)
    first = Price(
        sku=sku,
        amount=Decimal("19.9901"),
        currency="USD",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source="manual",
        approved=True,
    )
    second = Price(
        sku=sku,
        amount=Decimal("21.1255"),
        currency="USD",
        valid_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        source="manual",
        approved=False,
    )
    session.add_all([first, second])
    session.commit()
    session.expire_all()

    stored = session.get(SKU, sku.id)
    assert stored is not None
    assert {price.amount for price in stored.prices} == {
        Decimal("19.9901"),
        Decimal("21.1255"),
    }
    assert all(price.valid_from.utcoffset() == timedelta(0) for price in stored.prices)


@pytest.mark.parametrize(
    ("entity", "expected_constraint"),
    [
        (lambda: Brand(name="  "), "ck_brands_name_nonempty"),
        (
            lambda: SKU(
                product_id=uuid.uuid4(), size_value=Decimal("1"), size_unit=None
            ),
            "ck_skus_size_value_unit_pair",
        ),
        (
            lambda: Photo(
                sku_id=uuid.uuid4(),
                file_path="photo.jpg",
                checksum_sha256="short",
                original_filename="photo.jpg",
                mime_type="image/jpeg",
                file_size_bytes=100,
                width=10,
                height=10,
                role=PhotoRole.FRONT,
                is_original=True,
            ),
            "ck_photos_checksum_sha256_format",
        ),
        (
            lambda: Price(
                sku_id=uuid.uuid4(),
                amount=Decimal("-0.01"),
                currency="USD",
                valid_from=datetime.now(timezone.utc),
                source="manual",
                approved=False,
            ),
            "ck_prices_amount_nonnegative",
        ),
    ],
)
def test_database_check_constraints(
    session: Session, entity, expected_constraint: str
) -> None:
    session.add(entity())
    with pytest.raises(IntegrityError, match=expected_constraint):
        session.commit()


def test_required_foreign_key_constraint(session: Session) -> None:
    session.add(Product(brand_id=uuid.uuid4(), name="Orphan product"))
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        session.commit()


def test_create_schemas_validate_domain_inputs() -> None:
    brand = BrandCreate(name="  Nutra Bio  ")
    assert brand.name == "Nutra Bio"

    with pytest.raises(ValidationError):
        SKUCreate(product_id=uuid.uuid4(), size_value=Decimal("1.5"))

    with pytest.raises(ValidationError):
        PhotoCreate(
            sku_id=uuid.uuid4(),
            file_path="photo.jpg",
            checksum_sha256="not-a-sha256",
            is_original=True,
        )

    with pytest.raises(ValidationError):
        PriceCreate(
            sku_id=uuid.uuid4(),
            amount=Decimal("9.99"),
            currency="USD",
            valid_from=datetime(2026, 1, 1),
            source="manual",
        )

    price = PriceCreate(
        sku_id=uuid.uuid4(),
        amount=Decimal("9.9901"),
        currency="usd",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source="manual",
    )
    assert price.currency == "USD"
    assert price.amount == Decimal("9.9901")
