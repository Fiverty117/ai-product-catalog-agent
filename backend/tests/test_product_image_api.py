import hashlib
import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, Brand, DerivedImageReview, Job, Photo, Price, Product, SKU
from app.db.session import create_sqlite_engine, get_db
from app.domain.enums import DerivedImageReviewDecision, JobStatus, PhotoRole
from app.domain.schemas import ImageEnhancementJobPayload
from app.main import app
from app.services.categories import assign_product_category, create_category
from app.services.image_enhancement import IMAGE_ENHANCEMENT_CONFIG_VERSION, create_running_image_enhancement_run, complete_image_enhancement_run, hash_image_enhancement_parameters, store_processed_image
from app.services.image_presentation import create_derived_image_review, select_derived_image_for_photo


def image_bytes(color=(20, 30, 40)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 18), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'images.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    def override():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        yield factory, client, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()


def make_context(session: Session, tmp_path: Path):
    product = Product(name="Whey", brand=Brand(name="Landerfit"))
    sku = SKU(product=product, flavor="Vanilla")
    session.add(product)
    session.flush()
    category = create_category(session, name="Protein")
    assign_product_category(session, product_id=product.id, category_id=category.id, is_primary=True)
    session.add(Price(sku=sku, amount=Decimal("10"), currency="PYG", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), source="manual", approved=True))
    content = image_bytes()
    path = tmp_path / "original.png"
    path.write_bytes(content)
    photo = Photo(product=product, file_path=str(path), checksum_sha256=hashlib.sha256(content).hexdigest(), original_filename="original.png", mime_type="image/png", file_size_bytes=len(content), width=12, height=18, role=PhotoRole.FRONT, is_original=True)
    session.add(photo)
    session.flush()
    return product, sku, photo


def make_derived(session: Session, photo: Photo, tmp_path: Path, color):
    params = {"quality": "high", "output_format": "png", "size": "auto", "background": "auto"}
    payload = ImageEnhancementJobPayload(source_photo_id=photo.id, source_checksum_sha256=photo.checksum_sha256, provider="test", model="test", prompt_version="v1", config_version=IMAGE_ENHANCEMENT_CONFIG_VERSION, parameters=params)
    run = create_running_image_enhancement_run(session, payload=payload)
    stored = store_processed_image(image_bytes(color), processed_dir=tmp_path / "processed")
    return complete_image_enhancement_run(session, run, stored_image=stored)


def test_editorial_summary_review_selection_and_safe_media(store):
    factory, client, tmp_path = store
    with factory() as session:
        product, sku, photo = make_context(session, tmp_path)
        approved = make_derived(session, photo, tmp_path, (50, 60, 70))
        pending = make_derived(session, photo, tmp_path, (70, 80, 90))
        rejected = make_derived(session, photo, tmp_path, (90, 100, 110))
        create_derived_image_review(session, {"derived_image_id": approved.id, "decision": "approved"})
        create_derived_image_review(session, {"derived_image_id": rejected.id, "decision": "rejected"})
        session.commit()
        product_id, photo_id = product.id, photo.id
        approved_id, pending_id, rejected_id = approved.id, pending.id, rejected.id
        ownership = (photo.product_id, photo.sku_id)

    url = f"/api/products/{product_id}/images/editorial"
    summary = client.get(url)
    assert summary.status_code == 200
    body = summary.json()
    assert body["effective"]["presentation"] == "original"
    assert body["effective"]["preview_url"].startswith("/api/products/")
    assert body["original_preview_url"].endswith(str(photo_id))
    serialized = summary.text.lower()
    assert "file_path" not in serialized and "checksum" not in serialized and str(tmp_path).lower() not in serialized
    rows = {row["derived_image_id"]: row for row in body["derived_images"]}
    assert rows[str(approved_id)]["selectable"] is True
    assert rows[str(pending_id)]["review_state"] == "unreviewed" and rows[str(pending_id)]["selectable"] is False
    assert rows[str(rejected_id)]["review_state"] == "rejected" and rows[str(rejected_id)]["selectable"] is False
    assert client.get(body["original_preview_url"]).status_code == 200
    assert client.get(rows[str(rejected_id)]["preview_url"]).status_code == 200

    select_url = f"/api/products/{product_id}/images/photos/{photo_id}/presentation"
    assert client.put(select_url, json={"presentation": "derived", "derived_image_id": str(pending_id)}).status_code == 409
    assert client.put(select_url, json={"presentation": "derived", "derived_image_id": str(rejected_id)}).status_code == 409
    selected = client.put(select_url, json={"presentation": "derived", "derived_image_id": str(approved_id)})
    assert selected.status_code == 200, selected.text
    assert selected.json()["effective"]["derived_image_id"] == str(approved_id)
    assert selected.json()["product"]["hero"]["effective_derived_image_id"] == str(approved_id)
    assert next(row for row in selected.json()["derived_images"] if row["derived_image_id"] == str(approved_id))["selected"] is True
    original = client.put(select_url, json={"presentation": "original"})
    assert original.status_code == 200
    assert original.json()["effective"]["presentation"] == "original"
    with factory() as session:
        persisted = session.get(Photo, photo_id)
        assert (persisted.product_id, persisted.sku_id) == ownership

    approved_pending = client.post(f"/api/products/{product_id}/images/derived/{pending_id}/review", json={"decision": "approved"})
    assert approved_pending.status_code == 200
    pending_row = next(row for row in approved_pending.json()["derived_images"] if row["derived_image_id"] == str(pending_id))
    assert pending_row["review_state"] == "approved" and pending_row["selectable"] is True
    assert client.post(f"/api/products/{product_id}/images/derived/{pending_id}/review", json={"decision": "rejected"}).status_code == 409


def test_reject_pending_missing_asset_fallback_and_unknowns(store):
    factory, client, tmp_path = store
    with factory() as session:
        product, _, photo = make_context(session, tmp_path)
        pending = make_derived(session, photo, tmp_path, (1, 2, 3))
        missing = make_derived(session, photo, tmp_path, (4, 5, 6))
        selected = make_derived(session, photo, tmp_path, (7, 8, 9))
        create_derived_image_review(session, {"derived_image_id": missing.id, "decision": "approved"})
        create_derived_image_review(session, {"derived_image_id": selected.id, "decision": "approved"})
        select_derived_image_for_photo(session, photo_id=photo.id, derived_image_id=selected.id)
        Path(missing.file_path).unlink()
        Path(selected.file_path).unlink()
        session.commit()
        product_id, photo_id, pending_id, missing_id = product.id, photo.id, pending.id, missing.id

    rejected = client.post(f"/api/products/{product_id}/images/derived/{pending_id}/review", json={"decision": "rejected"})
    assert rejected.status_code == 200
    row = next(item for item in rejected.json()["derived_images"] if item["derived_image_id"] == str(pending_id))
    assert row["review_state"] == "rejected" and row["selectable"] is False
    summary = client.get(f"/api/products/{product_id}/images/editorial").json()
    assert summary["effective"]["presentation"] == "original"
    assert summary["effective"]["warnings"] == ["preferred_derived_asset_missing"]
    missing_row = next(item for item in summary["derived_images"] if item["derived_image_id"] == str(missing_id))
    assert missing_row["asset_available"] is False and missing_row["selectable"] is False and missing_row["preview_url"] is None
    assert client.get(f"/api/products/{product_id}/images/derived/{missing_id}").status_code == 404
    assert client.get(f"/api/products/{uuid.uuid4()}/images/editorial").status_code == 404
    assert client.put(f"/api/products/{product_id}/images/photos/{uuid.uuid4()}/presentation", json={"presentation": "original"}).status_code == 404
    assert client.post(f"/api/products/{product_id}/images/derived/{uuid.uuid4()}/review", json={"decision": "approved"}).status_code == 404


def test_sku_owned_front_photo_is_exposed_without_ownership_mutation(store):
    factory, client, tmp_path = store
    with factory() as session:
        product, sku, photo = make_context(session, tmp_path)
        photo.product = None
        photo.sku = sku
        session.commit()
        product_id, photo_id, sku_id = product.id, photo.id, sku.id
    body = client.get(f"/api/products/{product_id}/images/editorial").json()
    assert body["source_owner"] == "sku"
    assert body["source_sku_id"] == str(sku_id)
    assert client.put(f"/api/products/{product_id}/images/photos/{photo_id}/presentation", json={"presentation": "original"}).status_code == 200
    with factory() as session:
        persisted = session.get(Photo, photo_id)
        assert persisted.product_id is None and persisted.sku_id == sku_id


def test_explicit_generation_idempotency_status_retry_and_review_boundary(store):
    factory, client, tmp_path = store
    with factory() as session:
        product, _, photo = make_context(session, tmp_path)
        session.commit()
        product_id, photo_id = product.id, photo.id
    source_before = (tmp_path / "original.png").read_bytes()
    url = f"/api/products/{product_id}/images/photos/{photo_id}/enhancements"
    assert client.post(url).status_code == 422
    first = client.post(url, headers={"Idempotency-Key": "first-click"})
    assert first.status_code == 202, first.text
    job_id = first.json()["generation"]["job_id"]
    assert first.json()["editorial"]["can_generate"] is False
    assert first.json()["editorial"]["derived_images"] == []
    again = client.post(url, headers={"Idempotency-Key": "first-click"})
    assert again.status_code == 202 and again.json()["generation"]["job_id"] == job_id
    assert client.post(url, headers={"Idempotency-Key": "second-click"}).status_code == 409
    status_url = f"/api/products/{product_id}/images/enhancements/{job_id}"
    assert client.get(status_url).json()["status"] == "queued"
    with factory() as session:
        job = session.get(Job, uuid.UUID(job_id))
        job.status = JobStatus.RUNNING
        job.attempts = 1
        session.commit()
    assert client.get(status_url).json()["status"] == "running"
    with factory() as session:
        job = session.get(Job, uuid.UUID(job_id))
        job.status = JobStatus.FAILED
        job.last_error = "provider secret must stay private"
        session.commit()
    failed = client.get(status_url)
    assert failed.json()["status"] == "failed"
    assert "provider secret" not in failed.text
    retried = client.post(f"{status_url}/retry")
    assert retried.status_code == 202 and retried.json()["generation"]["job_id"] == job_id
    assert retried.json()["generation"]["status"] == "queued"
    with factory() as session:
        job = session.get(Job, uuid.UUID(job_id))
        job.status = JobStatus.FAILED
        job.attempts = job.max_attempts
        session.commit()
    replacement = client.post(f"{status_url}/retry")
    assert replacement.status_code == 202
    replacement_id = replacement.json()["generation"]["job_id"]
    assert replacement_id != job_id
    assert client.post(f"{status_url}/retry").status_code == 202
    with factory() as session:
        assert session.query(Job).filter(Job.job_type == "image.enhance.v1").count() == 2
        job = session.get(Job, uuid.UUID(replacement_id))
        payload = ImageEnhancementJobPayload.model_validate(job.payload)
        run = create_running_image_enhancement_run(session, payload=payload, job_id=job.id)
        derived = complete_image_enhancement_run(session, run, stored_image=store_processed_image(image_bytes((66, 77, 88)), processed_dir=tmp_path / "processed"))
        job.status = JobStatus.SUCCEEDED
        session.commit()
        derived_id = derived.id
    editorial = client.get(f"/api/products/{product_id}/images/editorial").json()
    assert editorial["effective"]["presentation"] == "original"
    assert editorial["derived_images"][0]["derived_image_id"] == str(derived_id)
    assert editorial["derived_images"][0]["review_state"] == "unreviewed"
    assert editorial["derived_images"][0]["selectable"] is False
    assert editorial["can_generate"] is True
    assert (tmp_path / "original.png").read_bytes() == source_before
    later = client.post(url, headers={"Idempotency-Key": "later-click"})
    assert later.status_code == 202 and later.json()["generation"]["job_id"] not in {job_id, replacement_id}
    assert client.get(f"/api/products/{uuid.uuid4()}/images/enhancements/{job_id}").status_code == 404


def test_generation_rejects_invalid_or_non_current_source_without_enqueuing(store):
    factory, client, tmp_path = store
    with factory() as session:
        product, _, photo = make_context(session, tmp_path)
        session.commit()
        product_id, photo_id = product.id, photo.id
    url = f"/api/products/{product_id}/images/photos/{photo_id}/enhancements"
    (tmp_path / "original.png").write_bytes(image_bytes((1, 1, 1)))
    assert client.get(f"/api/products/{product_id}/images/editorial").json()["can_generate"] is False
    assert client.post(url, headers={"Idempotency-Key": "invalid-source"}).status_code == 409
    assert client.post(f"/api/products/{product_id}/images/photos/{uuid.uuid4()}/enhancements", headers={"Idempotency-Key": "unknown"}).status_code == 404
    with factory() as session:
        assert session.query(Job).count() == 0
