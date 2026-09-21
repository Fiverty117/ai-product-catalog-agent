import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, Brand, Category, Price, Product, ProductCategory, ProductIdentityEdit, SKU, SKUFieldProvenance
from app.db.session import create_sqlite_engine, get_db
from app.domain.enums import FieldSource, FieldState, SKUFieldName
from app.main import app
from app.services.categories import assign_product_category, create_category
from app.services.prices import select_active_approved_price
from app.services.sku_field_provenance import LockedSKUFieldError, apply_sku_field_update


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'product-data.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    def override():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        yield factory, client
    app.dependency_overrides.clear()
    engine.dispose()


def seed(session: Session):
    brand = Brand(name="LANDERFIT")
    other_brand = Brand(name="OTHER")
    product = Product(brand=brand, name="Premium Whey")
    sibling = Product(brand=brand, name="Sibling")
    sku = SKU(product=product, flavor="Vanilla", size_value=Decimal("2"), size_unit="LB")
    other_sku = SKU(product=product, flavor="Chocolate", size_value=Decimal("2"), size_unit="LB")
    session.add_all([product, sibling, other_brand, sku, other_sku])
    session.flush()
    primary = create_category(session, name="Proteínas")
    secondary = create_category(session, name="Suplementos")
    assign_product_category(session, product_id=product.id, category_id=primary.id, is_primary=True)
    old = Price(sku=sku, amount=Decimal("360000.0000"), currency="PYG", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), source="manual", approved=True)
    session.add(old)
    session.commit()
    return product.id, sku.id, other_sku.id, sibling.id, other_brand.id, primary.id, secondary.id, old.id


def test_identity_brand_and_category_edits_are_safe_and_audited(store):
    factory, client = store
    with factory() as session:
        product_id, sku_id, _, sibling_id, other_brand_id, primary_id, secondary_id, _ = seed(session)
    base = f"/api/products/{product_id}/data"
    assert client.get(base).status_code == 200
    assert client.put(base + "/identity", json={"name": "  ", "brand_id": str(other_brand_id)}).status_code == 422
    assert client.put(base + "/identity", json={"name": "Sibling", "brand_id": client.get(base).json()["brand_id"]}).status_code == 409
    renamed = client.put(base + "/identity", json={"name": "Premium Whey Plus", "brand_id": str(other_brand_id)})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["product"]["product_name"] == "Premium Whey Plus"
    assert renamed.json()["product"]["brand_name"] == "OTHER"
    with factory() as session:
        edit = session.scalar(select(ProductIdentityEdit))
        assert edit.old_name == "Premium Whey" and edit.new_name == "Premium Whey Plus"
        assert session.get(Product, sibling_id).name == "Sibling"
        assert session.get(Brand, other_brand_id).name == "OTHER"
    chosen = client.put(base + "/categories", json={"primary_category_id": str(secondary_id), "secondary_category_ids": [str(primary_id)]})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["product"]["primary_category_id"] == str(secondary_id)
    assert chosen.json()["product"]["copy_state"] == "none"
    with factory() as session:
        rows = session.scalars(select(ProductCategory).where(ProductCategory.product_id == product_id)).all()
        assert sum(row.is_primary for row in rows) == 1
        assert all(row.source is FieldSource.HUMAN and row.locked for row in rows)
    assert client.put(base + "/categories", json={"primary_category_id": None, "secondary_category_ids": [str(primary_id)]}).status_code == 200
    assert client.get(base).json()["product"]["readiness"]["ready"] is False
    assert client.put(base + "/categories", json={"primary_category_id": str(primary_id), "secondary_category_ids": [str(primary_id)]}).status_code == 422
    assert client.get(f"/api/products/{uuid.uuid4()}/data").status_code == 404


def test_sku_edit_add_lock_and_price_history(store):
    factory, client = store
    with factory() as session:
        product_id, sku_id, other_sku_id, *_rest, old_price_id = seed(session)
    base = f"/api/products/{product_id}/data"
    fields = {"flavor": "Strawberry", "size_value": "1000", "size_unit": "g", "servings": 25, "external_sku": "STRAW-1"}
    assert client.put(base + f"/skus/{sku_id}", json={**fields, "size_unit": None}).status_code == 422
    edited = client.put(base + f"/skus/{sku_id}", json=fields)
    assert edited.status_code == 200, edited.text
    assert len(edited.json()["skus"]) == 2
    with factory() as session:
        sku = session.get(SKU, sku_id)
        assert sku.size_value == Decimal("1000") and sku.size_unit == "g"
        provenance = session.scalars(select(SKUFieldProvenance).where(SKUFieldProvenance.sku_id == sku_id)).all()
        assert {row.field_name for row in provenance} == set(SKUFieldName)
        assert all(row.source is FieldSource.HUMAN and row.state is FieldState.VERIFIED and row.locked for row in provenance)
        with pytest.raises(LockedSKUFieldError):
            apply_sku_field_update(session, sku=sku, field_name=SKUFieldName.FLAVOR, value="AI overwrite", source=FieldSource.MODEL)
    added = client.post(base + "/skus", json={"flavor": "Unflavored", "size_value": "1", "size_unit": "kg", "servings": None, "external_sku": None})
    assert added.status_code == 201, added.text
    assert len(added.json()["skus"]) == 3
    assert client.post(base + "/skus", json={"flavor": "Strawberry", "size_value": "1000", "size_unit": "g", "servings": 25, "external_sku": None}).status_code == 409
    change = client.post(base + f"/skus/{sku_id}/prices", json={"amount": "375000.1250", "currency": "PYG"})
    assert change.status_code == 201, change.text
    assert next(row for row in change.json()["skus"] if row["sku_id"] == str(sku_id))["active_price"]["amount"] == "375000.1250"
    with factory() as session:
        old = session.get(Price, old_price_id)
        assert old.amount == Decimal("360000.0000")
        latest = select_active_approved_price(session, sku_id=sku_id, currency="PYG", as_of=datetime.now(timezone.utc))
        assert latest.amount == Decimal("375000.1250")
        historical = select_active_approved_price(session, sku_id=sku_id, currency="PYG", as_of=datetime(2026, 1, 2, tzinfo=timezone.utc))
        assert historical.id == old_price_id
        assert select_active_approved_price(session, sku_id=other_sku_id, currency="PYG", as_of=datetime.now(timezone.utc)) is None
    assert client.post(base + f"/skus/{uuid.uuid4()}/prices", json={"amount": "3", "currency": "PYG"}).status_code == 404
