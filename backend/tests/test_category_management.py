import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, Brand, Category, Product, ProductCategory
from app.db.session import create_sqlite_engine, get_db
from app.main import app
from app.services.catalog_readiness import evaluate_product_catalog_readiness
from app.services.categories import assign_product_category
from app.services.category_suggestions import build_category_suggestion_input_snapshot


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'managed-categories.db'}")
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


def test_create_duplicate_reactivate_and_selectors(store):
    factory, client = store
    created = client.post("/api/categories", json={"name": "  Proteínas  ", "sort_order": 4})
    assert created.status_code == 201, created.text
    category_id = created.json()["id"]
    assert created.json()["name"] == "Proteínas"
    assert created.json()["identity_key"] == "proteínas"
    assert created.json()["is_active"] is True
    duplicate = client.post("/api/categories", json={"name": "PROTEÍNAS"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["existing_category_id"] == category_id
    assert client.post("/api/categories", json={"name": " "}).status_code == 422
    assert client.post("/api/categories", json={"name": "Other", "sort_order": -1}).status_code == 422
    with factory() as session:
        product = Product(name="Matcha", brand=Brand(name="Matcha Brand"))
        session.add(product)
        session.commit()
        product_id = product.id
        before = evaluate_product_catalog_readiness(session, product_id=product_id, currency="PYG")
        assert any(issue.code.value == "missing_primary_category" for issue in before.blockers)
    assert client.post(f"/api/categories/{category_id}/deactivate").status_code == 200
    inactive_duplicate = client.post("/api/categories", json={"name": " proteínas "})
    assert inactive_duplicate.status_code == 409
    assert inactive_duplicate.json()["detail"]["existing_category_active"] is False
    assert "Reactivate" in inactive_duplicate.json()["detail"]["message"]
    assert client.get("/api/categories").json()["items"] == []
    with factory() as session:
        assert all(item.id != uuid.UUID(category_id) for item in build_category_suggestion_input_snapshot(session, product_id).taxonomy)
    assert client.post(f"/api/categories/{category_id}/reactivate").json()["id"] == category_id
    selector = client.get(f"/api/products/{product_id}/data").json()["categories"]
    assert any(row["category_id"] == category_id for row in selector)
    with factory() as session:
        assert any(issue.code.value == "missing_primary_category" for issue in evaluate_product_catalog_readiness(
            session, product_id=product_id, currency="PYG"
        ).blockers)
    assigned = client.put(f"/api/products/{product_id}/data/categories", json={"primary_category_id": category_id, "secondary_category_ids": []})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["product"]["primary_category_id"] == category_id
    assert not any(issue["code"] == "missing_primary_category" for issue in assigned.json()["product"]["readiness"]["blockers"])


def test_usage_rename_order_and_safe_deactivation(store):
    factory, client = store
    first = client.post("/api/categories", json={"name": "Zeta", "sort_order": 10}).json()
    second = client.post("/api/categories", json={"name": "Alpha", "sort_order": 10}).json()
    assert [row["id"] for row in client.get("/api/categories").json()["items"]] == [second["id"], first["id"]]
    page = client.get("/api/categories", params={"status": "all", "limit": 1, "offset": 1}).json()
    assert page["total"] == page["all_total"] == 2
    assert [row["id"] for row in page["items"]] == [first["id"]]
    with factory() as session:
        products = [Product(name=f"Product {n}", brand=Brand(name=f"Brand {n}")) for n in range(3)]
        session.add_all(products)
        session.flush()
        assign_product_category(session, product_id=products[0].id, category_id=uuid.UUID(first["id"]), is_primary=True)
        assign_product_category(session, product_id=products[1].id, category_id=uuid.UUID(first["id"]))
        assign_product_category(session, product_id=products[2].id, category_id=uuid.UUID(first["id"]))
        session.commit()
    detail = client.get(f"/api/categories/{first['id']}").json()
    assert (detail["primary_product_count"], detail["secondary_product_count"], detail["total_product_count"]) == (1, 2, 3)
    assert len(detail["affected_products"]) == 3
    blocked = client.post(f"/api/categories/{first['id']}/deactivate")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "category_in_use"
    assert blocked.json()["detail"]["total_product_count"] == 3
    renamed = client.patch(f"/api/categories/{first['id']}", json={"name": "  Zeta supplements ", "sort_order": 0})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["id"] == first["id"]
    assert renamed.json()["identity_key"] == "zeta supplements"
    assert renamed.json()["sort_order"] == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProductCategory).where(ProductCategory.category_id == uuid.UUID(first["id"]))) == 3
    assert client.patch(f"/api/categories/{first['id']}", json={"name": "ALPHA"}).status_code == 409
    assert client.post(f"/api/categories/{second['id']}/deactivate").status_code == 200
    assert [row["id"] for row in client.get("/api/categories", params={"status": "inactive"}).json()["items"]] == [second["id"]]
    assert client.get("/api/categories", params={"search": "supplement", "status": "all"}).json()["total"] == 1
    assert client.get(f"/api/categories/{uuid.uuid4()}").status_code == 404


def test_concurrent_normalized_creation_has_single_winner(store):
    factory, client = store
    def submit(name):
        return client.post("/api/categories", json={"name": name}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, [" Matcha ", "MATCHA"]))
    assert sorted(statuses) == [201, 409]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Category).where(Category.identity_key == "matcha")) == 1
