from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, Brand, Product, SKU, SKUFieldProvenance
from app.db.session import create_sqlite_engine
from app.domain.enums import FieldSource, FieldState, SKUFieldName
from app.domain.schemas import SKUFieldProvenanceCreate, SKUFieldProvenanceRead
from app.services.sku_field_provenance import (
    InvalidSKUFieldError,
    LockedSKUFieldError,
    apply_sku_field_update,
)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'provenance.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_sku(session: Session) -> SKU:
    sku = SKU(product=Product(name="Whey", brand=Brand(name="Test Brand")))
    session.add(sku)
    session.flush()
    return sku


def test_provenance_creation_relationship_and_schema(session: Session) -> None:
    sku = make_sku(session)
    provenance = SKUFieldProvenance(
        sku=sku,
        field_name=SKUFieldName.FLAVOR,
        source=FieldSource.MODEL,
        confidence=Decimal("0.875000"),
        evidence="Front label says Vanilla",
        state=FieldState.EXTRACTED,
    )
    session.add(provenance)
    session.commit()

    assert provenance in sku.field_provenance
    assert provenance.locked is False
    assert provenance.confidence == Decimal("0.875000")
    assert SKUFieldProvenanceRead.model_validate(provenance).field_name is SKUFieldName.FLAVOR

    create_schema = SKUFieldProvenanceCreate(
        sku_id=sku.id,
        field_name=SKUFieldName.SERVINGS,
        source=FieldSource.OCR,
        confidence=Decimal("1"),
        state=FieldState.EXTRACTED,
    )
    assert create_schema.locked is False


def test_only_one_provenance_record_per_sku_and_field(session: Session) -> None:
    sku = make_sku(session)
    session.add_all(
        [
            SKUFieldProvenance(
                sku=sku,
                field_name=SKUFieldName.FLAVOR,
                source=FieldSource.MODEL,
                state=FieldState.EXTRACTED,
            ),
            SKUFieldProvenance(
                sku=sku,
                field_name=SKUFieldName.FLAVOR,
                source=FieldSource.OCR,
                state=FieldState.EXTRACTED,
            ),
        ]
    )

    with pytest.raises(
        IntegrityError,
        match=r"sku_field_provenance\.sku_id, sku_field_provenance\.field_name",
    ):
        session.commit()


@pytest.mark.parametrize("confidence", [Decimal("0"), Decimal("1")])
def test_confidence_inclusive_boundaries(
    session: Session, confidence: Decimal
) -> None:
    sku = make_sku(session)
    session.add(
        SKUFieldProvenance(
            sku=sku,
            field_name=SKUFieldName.FLAVOR,
            source=FieldSource.MODEL,
            confidence=confidence,
            state=FieldState.EXTRACTED,
        )
    )
    session.commit()


@pytest.mark.parametrize("confidence", [Decimal("-0.000001"), Decimal("1.000001")])
def test_confidence_outside_boundaries_is_rejected(
    session: Session, confidence: Decimal
) -> None:
    sku = make_sku(session)
    session.add(
        SKUFieldProvenance(
            sku=sku,
            field_name=SKUFieldName.FLAVOR,
            source=FieldSource.MODEL,
            confidence=confidence,
            state=FieldState.EXTRACTED,
        )
    )

    with pytest.raises(
        IntegrityError, match="ck_sku_field_provenance_confidence_range"
    ):
        session.commit()


def test_automated_update_is_allowed_while_unlocked(session: Session) -> None:
    sku = make_sku(session)
    provenance = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.FLAVOR,
        value="Vanilla",
        source=FieldSource.MODEL,
        confidence=Decimal("0.720000"),
        evidence="Visible on front label",
    )
    session.flush()

    updated = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.FLAVOR,
        value="French Vanilla",
        source=FieldSource.OCR,
        confidence=Decimal("0.910000"),
        evidence="OCR crop",
    )

    assert updated is provenance
    assert sku.flavor == "French Vanilla"
    assert updated.source is FieldSource.OCR
    assert updated.state is FieldState.EXTRACTED
    assert updated.locked is False


def test_size_value_and_unit_can_be_applied_in_one_transaction(session: Session) -> None:
    sku = make_sku(session)
    apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.SIZE_VALUE,
        value=Decimal("2.500000"),
        source=FieldSource.RULE,
    )
    apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.SIZE_UNIT,
        value="kg",
        source=FieldSource.RULE,
    )

    session.flush()

    assert sku.size_value == Decimal("2.500000")
    assert sku.size_unit == "kg"


def test_automated_update_is_rejected_when_locked(session: Session) -> None:
    sku = make_sku(session)
    provenance = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.FLAVOR,
        value="Chocolate",
        source=FieldSource.HUMAN,
        evidence="Confirmed by reviewer",
    )
    session.flush()

    with pytest.raises(LockedSKUFieldError):
        apply_sku_field_update(
            session,
            sku=sku,
            field_name=SKUFieldName.FLAVOR,
            value="Vanilla",
            source=FieldSource.MODEL,
            confidence=Decimal("0.990000"),
        )

    assert sku.flavor == "Chocolate"
    assert provenance.source is FieldSource.HUMAN
    assert provenance.evidence == "Confirmed by reviewer"


def test_human_override_updates_canonical_value_and_locks(session: Session) -> None:
    sku = make_sku(session)
    provenance = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.EXTERNAL_SKU,
        value="MODEL-123",
        source=FieldSource.MODEL,
        confidence=Decimal("0.800000"),
    )
    session.flush()

    overridden = apply_sku_field_update(
        session,
        sku=sku,
        field_name=SKUFieldName.EXTERNAL_SKU,
        value="HUMAN-456",
        source=FieldSource.HUMAN,
        evidence="Verified against inventory label",
    )

    assert overridden is provenance
    assert sku.external_sku == "HUMAN-456"
    assert overridden.source is FieldSource.HUMAN
    assert overridden.state is FieldState.VERIFIED
    assert overridden.locked is True
    assert overridden.confidence is None


def test_invalid_field_name_is_rejected_without_mutating_sku(session: Session) -> None:
    sku = make_sku(session)

    with pytest.raises(InvalidSKUFieldError):
        apply_sku_field_update(
            session,
            sku=sku,
            field_name="price",
            value=Decimal("10.00"),
            source=FieldSource.IMPORT,
        )

    assert sku.field_provenance == []
