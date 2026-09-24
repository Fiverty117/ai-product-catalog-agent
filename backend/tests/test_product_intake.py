import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.vision import VisionExtractionResponse
from app.api.photos import get_originals_dir
from app.db import Base, Brand, CatalogBuild, CatalogSnapshot, Category, ExtractionRun, ImageEnhancementRun, Job, Photo, Price, Product, ProductCategory, ProductCopyRun, ProductIntakePromotion, SKU, SKUFieldProvenance
from app.db.session import create_sqlite_engine, get_db
from app.domain.schemas import ProductExtractionResult
from app.main import app
from app.services.extraction import PRODUCT_EXTRACTION_JOB_TYPE
from app.workers.extraction_handler import ProductExtractionJobHandler
from app.workers.job_worker import JobWorker


def picture(color: str = "red") -> bytes:
    out = BytesIO()
    Image.new("RGB", (10, 12), color).save(out, format="PNG")
    return out.getvalue()


def observation(value):
    return {"value": value, "state": "extracted" if value is not None else "not_present"}


class FakeProvider:
    name = "openai"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def extract(self, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider secret=unsafe")
        assert len(request.images) == 2
        return VisionExtractionResponse(structured_result=ProductExtractionResult.model_validate({
            "brand_name": observation("Test Brand"), "product_name": observation("Test Product"),
            "flavor": observation("Vanilla"), "size_value": observation("750"),
            "size_unit": observation("g"), "servings": observation(30),
        }), usage={})


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'intake.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    originals = tmp_path / "originals"

    def db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_originals_dir] = lambda: originals
    with TestClient(app) as client:
        yield client, factory, originals
    app.dependency_overrides.clear()
    engine.dispose()


def make_item(client):
    return client.post("/api/product-intake/items", files=[
        ("images", ("front.png", picture(), "image/png")),
        ("images", ("back.png", picture("blue"), "image/png")),
    ])


def assert_no_canonical(factory):
    with factory() as session:
        for model in (Brand, Product, SKU, Price, Category, ProductCategory):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_upload_order_media_draft_and_canonical_safety(store):
    client, factory, originals = store
    created = make_item(client)
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["status"] == "draft"
    assert [p["position"] for p in item["photos"]] == [0, 1]
    assert [p["is_primary"] for p in item["photos"]] == [True, False]
    assert client.get(item["photos"][0]["image_url"]).content == picture()
    assert str(originals) not in created.text
    with factory() as session:
        photos = session.scalars(select(Photo)).all()
        assert len(photos) == 2
        assert all(p.product_id is None and p.sku_id is None for p in photos)
        assert {p.checksum_sha256 for p in photos} == {hashlib.sha256(picture(c)).hexdigest() for c in ("red", "blue")}
    assert_no_canonical(factory)

    draft = item["draft"] | {"brand_name": "Hand Brand", "product_name": "Hand Product", "skus": [
        {"flavor": "Berry", "size_value": "750", "size_unit": "g", "servings": 30, "external_sku": "H-1"}
    ]}
    saved = client.put(f"/api/product-intake/items/{item['id']}/draft", json=draft)
    assert saved.status_code == 200, saved.text
    assert client.get(f"/api/product-intake/items/{item['id']}").json()["draft"]["brand_name"] == "Hand Brand"
    assert_no_canonical(factory)
    assert client.put(f"/api/product-intake/items/{item['id']}/draft", json=draft | {"skus": [{"size_value": "1"}]}).status_code == 422
    assert client.put(f"/api/product-intake/items/{item['id']}/draft", json=draft | {"skus": [{"servings": 0}]}).status_code == 422
    assert client.put(f"/api/product-intake/items/{item['id']}/primary-photo", json={"photo_id": item["photos"][1]["id"]}).json()["photos"][1]["is_primary"]
    assert client.get(f"/api/product-intake/items/{item['id']}/photos/{uuid.uuid4()}/image").status_code == 404


def test_extraction_idempotency_success_and_human_rerun(store):
    client, factory, _ = store
    item = make_item(client).json()
    key = str(uuid.uuid4())
    url = f"/api/product-intake/items/{item['id']}/extractions"
    first = client.post(url, headers={"Idempotency-Key": key})
    again = client.post(url, headers={"Idempotency-Key": key})
    assert first.status_code == again.status_code == 202
    assert first.json()["extraction"]["job_id"] == again.json()["extraction"]["job_id"]
    assert first.json()["status"] == "queued"
    assert client.post(url, headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 409
    provider = FakeProvider()
    worker = JobWorker(factory, {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(factory, provider)}, accepted_job_types={PRODUCT_EXTRACTION_JOB_TYPE})
    worker.run_once()
    done = client.get(f"/api/product-intake/items/{item['id']}").json()
    assert done["status"] == "review_required"
    assert done["draft"]["brand_name"] == "Test Brand"
    assert done["draft"]["skus"][0]["size_value"] == "750"
    first_run = done["extraction"]["run_id"]
    with factory() as session:
        unchanged = session.get(ExtractionRun, uuid.UUID(first_run)).structured_result.copy()
    edited = done["draft"] | {"brand_name": "Human Brand"}
    assert client.put(f"/api/product-intake/items/{item['id']}/draft", json=edited).status_code == 200
    assert client.post(url, headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 202
    assert client.post(url, headers={"Idempotency-Key": key}).status_code == 409
    worker.run_once()
    rerun = client.get(f"/api/product-intake/items/{item['id']}").json()
    assert rerun["draft"]["brand_name"] == "Human Brand"
    assert rerun["extraction"]["newer_result_available"] is True
    with factory() as session:
        assert session.get(ExtractionRun, uuid.UUID(first_run)).structured_result == unchanged
        assert session.scalar(select(func.count()).select_from(ExtractionRun)) == 2
    assert_no_canonical(factory)


def test_failures_invalid_upload_and_list(store):
    client, factory, _ = store
    assert client.post("/api/product-intake/items", files=[("images", ("bad.png", b"bad", "image/png"))]).status_code == 422
    empty = client.post("/api/product-intake/items").json()
    assert client.post(f"/api/product-intake/items/{empty['id']}/extractions", headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 422
    assert client.get(f"/api/product-intake/items/{uuid.uuid4()}").status_code == 404
    item = make_item(client).json()
    assert client.get("/api/product-intake/items").json()["items"][0]["id"] == item["id"]
    client.post(f"/api/product-intake/items/{item['id']}/extractions", headers={"Idempotency-Key": str(uuid.uuid4())})
    with factory() as session:
        job = session.get(Job, uuid.UUID(client.get(f"/api/product-intake/items/{item['id']}").json()["extraction"]["job_id"]))
        job.status = "running"
        session.commit()
    assert client.get(f"/api/product-intake/items/{item['id']}").json()["status"] == "running"
    with factory() as session:
        job = session.get(Job, job.id)
        job.status = "failed"
        session.commit()
    assert client.get(f"/api/product-intake/items/{item['id']}").json()["status"] == "failed"
    assert_no_canonical(factory)


def test_identical_bytes_do_not_merge_intake_items(store):
    client, factory, originals = store
    first = client.post("/api/product-intake/items", files=[("images", ("first.png", picture(), "image/png"))]).json()
    second = client.post("/api/product-intake/items", files=[("images", ("second.png", picture(), "image/png"))]).json()
    assert first["id"] != second["id"]
    assert first["photos"][0]["id"] != second["photos"][0]["id"]
    assert len(list(originals.rglob("*.png"))) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 2
    assert_no_canonical(factory)


def test_worker_failure_is_sanitized_and_new_action_can_retry(store):
    client, factory, _ = store
    item = make_item(client).json()
    url = f"/api/product-intake/items/{item['id']}/extractions"
    queued = client.post(url, headers={"Idempotency-Key": str(uuid.uuid4())}).json()
    with factory() as session:
        job = session.get(Job, uuid.UUID(queued["extraction"]["job_id"]))
        job.max_attempts = 1
        session.commit()
    provider = FakeProvider(fail=True)
    worker = JobWorker(factory, {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(factory, provider)}, accepted_job_types={PRODUCT_EXTRACTION_JOB_TYPE})
    worker.run_once()
    failed = client.get(f"/api/product-intake/items/{item['id']}").json()
    assert failed["status"] == "failed"
    assert "secret" not in failed["extraction"]["error"]
    assert client.post(url, headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 202
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExtractionRun)) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == 2
    assert_no_canonical(factory)


def reviewed_item(client, *, brand="New Brand", product="Matcha", skus=None):
    item = make_item(client).json()
    draft = item["draft"] | {
        "brand_name": brand, "product_name": product,
        "primary_category_name": "Suggested category",
        "skus": skus if skus is not None else [
            {"flavor": "Plain", "size_value": "1000", "size_unit": "g", "servings": 30, "external_sku": "MATCHA-1"},
        ],
    }
    response = client.put(f"/api/product-intake/items/{item['id']}/draft", json=draft)
    assert response.status_code == 200, response.text
    return response.json()


def promotion_body(*, category=None, secondary=(), prices=(), key=None):
    return {
        "idempotency_key": key or str(uuid.uuid4()),
        "primary_category_id": category,
        "secondary_category_ids": list(secondary),
        "sku_prices": list(prices),
    }


def test_promotion_creates_one_graph_and_retries_idempotently(store):
    client, factory, originals = store
    with factory() as session:
        existing_brand = Brand(name="NEW BRAND")
        from app.services.categories import create_category
        primary = create_category(session, name="Tea")
        secondary = create_category(session, name="Supplements")
        session.add(existing_brand)
        session.commit()
        brand_id, primary_id, secondary_id = existing_brand.id, primary.id, secondary.id
    item = reviewed_item(client, skus=[
        {"flavor": "Plain", "size_value": "1000", "size_unit": "g", "servings": 30, "external_sku": "MATCHA-1"},
        {"flavor": "Berry", "size_value": "1", "size_unit": "kg", "servings": 60, "external_sku": "MATCHA-2"},
    ])
    body = promotion_body(category=str(primary_id), secondary=[str(secondary_id)], prices=[
        {"intake_sku_index": 0, "amount": "125000.50", "currency": "PYG"},
        {"intake_sku_index": 1, "amount": "149000", "currency": "PYG"},
    ])
    url = f"/api/product-intake/items/{item['id']}/promotion"
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["brand_reused"] is True
    assert result["brand_id"] == str(brand_id)
    assert result["product"]["readiness"]["ready"] is True
    assert len(result["sku_ids"]) == 2
    assert client.post(url, json=body).json()["product_id"] == result["product_id"]
    assert client.post(url, json=body | {"idempotency_key": str(uuid.uuid4())}).status_code == 409
    assert client.post(url, json=body | {"sku_prices": []}).status_code == 409
    detail = client.get(f"/api/product-intake/items/{item['id']}").json()
    assert detail["status"] == "promoted"
    assert detail["draft"]["primary_category_name"] == "Suggested category"
    assert detail["promotion"]["product_id"] == result["product_id"]
    assert client.get(item["photos"][0]["image_url"]).status_code == 200
    assert client.put(f"/api/product-intake/items/{item['id']}/draft", json=item["draft"]).status_code == 409
    assert client.post(f"/api/product-intake/items/{item['id']}/extractions", headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 409
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Brand)) == 1
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(SKU)) == 2
        assert session.scalar(select(func.count()).select_from(Price)) == 2
        assert session.scalar(select(func.count()).select_from(Category)) == 2
        assert session.scalar(select(func.count()).select_from(ProductCategory)) == 2
        assignments = session.scalars(select(ProductCategory)).all()
        assert sum(row.is_primary for row in assignments) == 1
        assert {row.category_id for row in assignments} == {primary_id, secondary_id}
        assert session.scalar(select(func.count()).select_from(ProductIntakePromotion)) == 1
        photos = session.scalars(select(Photo).order_by(Photo.created_at, Photo.id)).all()
        assert all(photo.product_id == uuid.UUID(result["product_id"]) and photo.sku_id is None for photo in photos)
        assert sum(photo.role.value == "front" for photo in photos) == 1
        assert {Path(photo.file_path).read_bytes() for photo in photos} == {picture(), picture("blue")}
        provenances = session.scalars(select(SKUFieldProvenance)).all()
        assert provenances and all(row.source.value == "human" and row.state.value == "verified" and row.locked for row in provenances)
        prices = session.scalars(select(Price)).all()
        assert all(price.approved and price.source == "human" for price in prices)
        assert {price.amount for price in prices} == {Decimal("125000.5000"), Decimal("149000.0000")}
        assert all(price.valid_from is not None for price in prices)
        for model in (CatalogSnapshot, CatalogBuild, ProductCopyRun, ImageEnhancementRun):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_promotion_rejects_collision_and_rolls_back(store):
    client, factory, _ = store
    item = reviewed_item(client)
    with factory() as session:
        brand = Brand(name="New Brand")
        product = Product(brand=brand, name="MATCHA")
        session.add(product)
        session.commit()
    response = client.post(f"/api/product-intake/items/{item['id']}/promotion", json=promotion_body())
    assert response.status_code == 409, response.text
    assert "canonical Product" in response.json()["detail"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(ProductIntakePromotion)) == 0
        assert session.scalar(select(func.count()).select_from(SKU)) == 0
        assert session.scalar(select(func.count()).select_from(Price)) == 0
    assert client.get(f"/api/product-intake/items/{item['id']}").json()["status"] == "draft"


def test_promotion_without_category_or_price_is_not_ready(store):
    client, factory, _ = store
    item = reviewed_item(client)
    result = client.post(f"/api/product-intake/items/{item['id']}/promotion", json=promotion_body()).json()
    assert result["brand_reused"] is False
    codes = {issue["code"] for issue in result["product"]["readiness"]["blockers"]}
    assert {"missing_primary_category", "no_publishable_skus"} <= codes
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Price)) == 0
        assert session.scalar(select(func.count()).select_from(Category)) == 0


def test_promotion_rejects_invalid_choices_and_zero_variants(store):
    client, factory, _ = store
    item = reviewed_item(client, skus=[])
    url = f"/api/product-intake/items/{item['id']}/promotion"
    assert client.post(url, json=promotion_body()).status_code == 422
    assert client.get(f"/api/product-intake/items/{uuid.uuid4()}/promotion").status_code == 404
    item = reviewed_item(client, brand="Another Brand", product="Other Product")
    url = f"/api/product-intake/items/{item['id']}/promotion"
    assert client.post(url, json=promotion_body(category=str(uuid.uuid4()))).status_code == 404
    assert client.post(url, json=promotion_body(prices=[{"intake_sku_index": 0, "amount": "0", "currency": "PYG"}])).status_code == 422
    assert client.post(url, json=promotion_body(prices=[{"intake_sku_index": 0, "amount": 100, "currency": "PYG"}])).status_code == 422
    assert client.post(url, json=promotion_body(prices=[{"intake_sku_index": 7, "amount": "100", "currency": "PYG"}])).status_code == 422
    same_id = str(uuid.uuid4())
    assert client.post(url, json=promotion_body(category=same_id, secondary=[same_id])).status_code == 422
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Brand)) == 0
        assert session.scalar(select(func.count()).select_from(Product)) == 0
        assert session.scalar(select(func.count()).select_from(ProductIntakePromotion)) == 0


def test_duplicate_variant_rolls_back_entire_graph(store):
    client, factory, _ = store
    item = reviewed_item(client, skus=[
        {"flavor": "Vanilla", "size_value": "1000", "size_unit": "g", "servings": 30, "external_sku": "A"},
        {"flavor": "vanilla", "size_value": "1000.0", "size_unit": "G", "servings": 30, "external_sku": "B"},
    ])
    response = client.post(f"/api/product-intake/items/{item['id']}/promotion", json=promotion_body())
    assert response.status_code == 409, response.text
    with factory() as session:
        for model in (Brand, Product, SKU, Price, ProductCategory, ProductIntakePromotion):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert all(photo.product_id is None for photo in session.scalars(select(Photo)).all())
    assert client.get(f"/api/product-intake/items/{item['id']}").json()["status"] == "draft"


def test_similar_brand_requires_human_resolution(store):
    client, factory, _ = store
    with factory() as session:
        session.add(Brand(name="NewBrand"))
        session.commit()
    item = reviewed_item(client, brand="New Brand")
    response = client.post(f"/api/product-intake/items/{item['id']}/promotion", json=promotion_body())
    assert response.status_code == 409
    assert "similar canonical Brand" in response.json()["detail"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 0


def test_concurrent_same_action_creates_one_product(store):
    client, factory, _ = store
    item = reviewed_item(client)
    body = promotion_body()
    url = f"/api/product-intake/items/{item['id']}/promotion"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json=body), range(2)))
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["product_id"] == responses[1].json()["product_id"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(SKU)) == 1
        assert session.scalar(select(func.count()).select_from(ProductIntakePromotion)) == 1


def test_promotion_preserves_extraction_observation(store):
    client, factory, _ = store
    item = make_item(client).json()
    extraction = client.post(
        f"/api/product-intake/items/{item['id']}/extractions",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert extraction.status_code == 202
    worker = JobWorker(factory, {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(factory, FakeProvider())}, accepted_job_types={PRODUCT_EXTRACTION_JOB_TYPE})
    worker.run_once()
    detail = client.get(f"/api/product-intake/items/{item['id']}").json()
    assert detail["status"] == "review_required"
    run_id = uuid.UUID(detail["extraction"]["run_id"])
    with factory() as session:
        observed = session.get(ExtractionRun, run_id).structured_result.copy()
    response = client.post(f"/api/product-intake/items/{item['id']}/promotion", json=promotion_body())
    assert response.status_code == 201, response.text
    with factory() as session:
        run = session.get(ExtractionRun, run_id)
        assert run.structured_result == observed
        assert {photo.id for photo in run.photos} == {uuid.UUID(p["id"]) for p in item["photos"]}
        assert session.scalar(select(func.count()).select_from(ExtractionRun)) == 1
    after = client.get(f"/api/product-intake/items/{item['id']}").json()
    assert after["draft"] == detail["draft"]
    assert after["extraction"]["run_id"] == str(run_id)
