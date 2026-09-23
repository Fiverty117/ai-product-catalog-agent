import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import app.api.catalog_builder as catalog_builder_api
from app.db import (
    Base,
    Brand,
    CatalogArtifact,
    CatalogBrandProfile,
    CatalogBuild,
    Category,
    CatalogSnapshot,
    Job,
    Photo,
    Price,
    Product,
    SKU,
)
from app.db.session import create_sqlite_engine, get_db
from app.domain.enums import (
    DerivedImageReviewDecision,
    JobStatus,
    PhotoRole,
    ProductCopyReviewDecision,
)
from app.domain.schemas import (
    CatalogBuildCreate,
    DerivedImageReviewCreate,
    ImageEnhancementJobPayload,
    ProductCopyJobPayload,
    ProductCopyReviewRequest,
)
from app.main import app
from app.rendering.catalog_pdf import CatalogPdfRenderResult
from app.services.catalog_builds import (
    create_catalog_build,
    resolve_catalog_build_artifact_pdf,
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
from app.services.product_copy import (
    PRODUCT_COPY_SCHEMA_VERSION,
    build_product_copy_input_snapshot,
    build_product_copy_source_fingerprint,
    create_running_product_copy_run,
    mark_product_copy_run_succeeded,
)
from app.services.product_copy_review import apply_product_copy_review
from app.workers.catalog_render_handler import catalog_render_handlers
from app.workers.job_worker import JobWorker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT / "templates" / "grabelan"
AS_OF = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class FakeRenderer:
    def __init__(self, pdf_bytes: bytes):
        self.pdf_bytes = pdf_bytes

    def render(self, html, config):
        return CatalogPdfRenderResult(
            pdf_bytes=self.pdf_bytes,
            engine="chromium",
            engine_version="test",
        )


def _valid_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def build_store(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    catalogs_dir = storage_root / "catalogs"
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'build.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        with factory() as session:
            yield session

    real_create = create_catalog_build
    real_resolve = resolve_catalog_build_artifact_pdf
    monkeypatch.setattr(
        catalog_builder_api,
        "create_catalog_build",
        lambda session, request: real_create(
            session,
            request,
            storage_root=storage_root,
            template_root=TEMPLATE_ROOT,
            clock=lambda: AS_OF,
        ),
    )
    monkeypatch.setattr(
        catalog_builder_api,
        "resolve_catalog_build_artifact_pdf",
        lambda session, artifact_id: real_resolve(
            session, artifact_id, catalogs_dir=catalogs_dir
        ),
    )
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield factory, client, storage_root, catalogs_dir
    app.dependency_overrides.clear()
    engine.dispose()


def _image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 36), (220, 225, 216)).save(output, format="PNG")
    return output.getvalue()


def _add_copy(session, product: Product, text: str) -> None:
    snapshot = build_product_copy_input_snapshot(session, product.id)
    payload = ProductCopyJobPayload(
        product_id=product.id,
        copy_type="short_description",
        provider="test",
        model="test",
        prompt_version="product-copy-v1",
        schema_version=PRODUCT_COPY_SCHEMA_VERSION,
        parameters={},
        source_fingerprint=build_product_copy_source_fingerprint(snapshot),
        input_snapshot=snapshot,
    )
    run = create_running_product_copy_run(session, payload=payload, started_at=AS_OF)
    mark_product_copy_run_succeeded(
        session,
        run,
        structured_result={"short_description": text},
        completed_at=AS_OF,
    )
    apply_product_copy_review(
        session,
        ProductCopyReviewRequest(
            product_copy_run_id=run.id,
            decision=ProductCopyReviewDecision.APPROVED,
        ),
        applied_at=AS_OF,
    )


def _seed_ready(session, storage_root: Path):
    product = Product(name="Premium Whey", brand=Brand(name="Landerfit"))
    session.add(product)
    session.flush()
    category = create_category(session, name="Proteínas", sort_order=10)
    assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    sku = SKU(product=product, external_sku="LANDER-WHEY-2LB", flavor="Vanilla")
    session.add(sku)
    session.flush()
    price = Price(
        sku=sku,
        amount=Decimal("350000"),
        currency="PYG",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source="human",
        approved=True,
    )
    content = _image_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    path = storage_root / "originals" / checksum[:2] / f"{checksum}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    photo = Photo(
        product=product,
        file_path=str(path),
        checksum_sha256=checksum,
        original_filename="front.png",
        mime_type="image/png",
        file_size_bytes=len(content),
        width=24,
        height=36,
        role=PhotoRole.FRONT,
        is_original=True,
    )
    profile = CatalogBrandProfile(
        key="grabelan",
        display_name="Grabelan Natural Market",
        primary_color="#183D2F",
        accent_color="#C89B3C",
        is_active=True,
    )
    session.add_all([price, photo, profile])
    session.flush()
    _add_copy(session, product, "Descripción humana aprobada.")
    session.commit()
    return product, sku, price, photo, profile


def _request(product: Product, profile: CatalogBrandProfile, key="action-1"):
    return {
        "product_ids": [str(product.id)],
        "catalog_brand_profile_id": str(profile.id),
        "layout_key": "classic",
        "layout_version": "1",
        "currency": "PYG",
        "idempotency_key": key,
    }


def _worker(factory, storage_root, catalogs_dir, pdf_bytes):
    return JobWorker(
        factory,
        catalog_render_handlers(
            factory,
            FakeRenderer(pdf_bytes),
            storage_root=storage_root,
            catalogs_dir=catalogs_dir,
            template_root=TEMPLATE_ROOT,
        ),
        accepted_job_types={"catalog.render.v2"},
    )


def test_create_is_authoritative_transactional_and_idempotent(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, sku, price, photo, profile = _seed_ready(session, storage_root)
        product_id, profile_id = product.id, profile.id
        other = Product(name="Other Product", brand=product.brand)
        session.add(other)
        session.flush()
        assign_product_category(
            session,
            product_id=other.id,
            category_id=session.scalar(select(Category.id)),
            is_primary=True,
        )
        other_sku = SKU(product=other, external_sku="OTHER-1")
        session.add(other_sku)
        session.flush()
        session.add(Price(
            sku=other_sku,
            amount=Decimal("100000"),
            currency="PYG",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source="human",
            approved=True,
        ))
        session.add(Photo(
            product=other,
            file_path=photo.file_path,
            checksum_sha256=photo.checksum_sha256,
            original_filename="other.png",
            mime_type=photo.mime_type,
            file_size_bytes=photo.file_size_bytes,
            width=photo.width,
            height=photo.height,
            role=PhotoRole.FRONT,
            is_original=True,
        ))
        session.commit()

    response = client.post("/api/catalog-builder/builds", json=_request(product, profile))
    assert response.status_code == 200
    created = response.json()
    assert created["status"] == "queued"
    assert created["product_count"] == 1
    assert created["catalog_brand_display_name"] == "Grabelan Natural Market"
    assert created["layout_display_label"] == "Classic"

    repeated = client.post("/api/catalog-builder/builds", json=_request(product, profile))
    assert repeated.status_code == 200
    assert repeated.json()["id"] == created["id"]
    reused_key = _request(product, profile)
    reused_key["layout_key"] = "dense"
    assert client.post("/api/catalog-builder/builds", json=reused_key).status_code == 409
    newer = client.post(
        "/api/catalog-builder/builds", json=_request(product, profile, "action-2")
    )
    assert newer.status_code == 200
    assert newer.json()["id"] != created["id"]

    with factory() as session:
        assert session.scalar(select(func.count(CatalogBuild.id))) == 2
        assert session.scalar(select(func.count(CatalogSnapshot.id))) == 2
        assert session.scalar(select(func.count(Job.id))) == 2
        snapshot = session.get(CatalogSnapshot, uuid.UUID(created["catalog_snapshot_id"]))
        frozen = snapshot.payload["sections"][0]["products"][0]
        assert frozen["source_product_id"] == str(product_id)
        assert len(snapshot.payload["sections"][0]["products"]) == 1
        assert frozen["product_name"] == "Premium Whey"
        assert frozen["short_description"] == "Descripción humana aprobada."
        assert frozen["hero"]["source_photo_id"] == str(photo.id)
        assert frozen["variants"][0]["source_sku_id"] == str(sku.id)
        assert frozen["variants"][0]["price"]["source_price_id"] == str(price.id)
        assert snapshot.payload["sections"][0]["products"][0]["variants"][0]["price"]["amount"] == "350000"
        assert profile_id == uuid.UUID(created["catalog_brand_profile_id"])

        session.get(Product, product_id).name = "Changed live name"
        session.get(Price, price.id).amount = Decimal("999999")
        session.get(CatalogBrandProfile, profile_id).display_name = "Changed live publisher"
        session.commit()
        assert snapshot.payload["sections"][0]["products"][0]["product_name"] == "Premium Whey"
        assert snapshot.payload["sections"][0]["products"][0]["variants"][0]["price"]["amount"] == "350000"
    assert client.get(f"/api/catalog-builder/builds/{created['id']}").json()[
        "catalog_brand_display_name"
    ] == "Grabelan Natural Market"
    replay_after_live_edits = client.post(
        "/api/catalog-builder/builds", json=_request(product, profile)
    )
    assert replay_after_live_edits.status_code == 200
    assert replay_after_live_edits.json()["id"] == created["id"]


def test_build_freezes_selected_approved_enhanced_image(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, _, photo, profile = _seed_ready(session, storage_root)
        payload = ImageEnhancementJobPayload(
            source_photo_id=photo.id,
            source_checksum_sha256=photo.checksum_sha256,
            provider="test",
            model="test",
            prompt_version="product-image-enhancement-v1",
            config_version="image-enhancement-config-v1",
            parameters={"quality": "high"},
        )
        run = create_running_image_enhancement_run(session, payload=payload)
        stored = store_processed_image(
            _image_bytes(), processed_dir=storage_root / "processed"
        )
        derived = complete_image_enhancement_run(session, run, stored_image=stored)
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
        session.commit()

    created = client.post(
        "/api/catalog-builder/builds", json=_request(product, profile)
    )
    assert created.status_code == 200
    with factory() as session:
        snapshot = session.get(
            CatalogSnapshot, uuid.UUID(created.json()["catalog_snapshot_id"])
        )
        frozen_hero = snapshot.payload["sections"][0]["products"][0]["hero"]
        assert frozen_hero["presentation_type"] == "derived"
        assert frozen_hero["source_derived_image_id"] == str(derived.id)
        assert frozen_hero["presentation_asset"]["storage_relative_path"].startswith("processed/")
        use_original_photo_presentation(session, photo_id=photo.id)
        session.commit()
        assert snapshot.payload["sections"][0]["products"][0]["hero"] == frozen_hero


def test_uncommitted_create_rolls_back_snapshot_job_and_build(build_store):
    factory, _, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        request = CatalogBuildCreate.model_validate(_request(product, profile))

    with factory() as session:
        create_catalog_build(
            session,
            request,
            storage_root=storage_root,
            template_root=TEMPLATE_ROOT,
            clock=lambda: AS_OF,
        )
        session.rollback()

    with factory() as session:
        assert session.scalar(select(func.count(CatalogBuild.id))) == 0
        assert session.scalar(select(func.count(CatalogSnapshot.id))) == 0
        assert session.scalar(select(func.count(Job.id))) == 0


def test_concurrent_delivery_of_one_action_reuses_one_build(build_store):
    factory, _, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        request = CatalogBuildCreate.model_validate(_request(product, profile))

    def deliver():
        with factory() as session:
            build = create_catalog_build(
                session,
                request,
                storage_root=storage_root,
                template_root=TEMPLATE_ROOT,
                clock=lambda: AS_OF,
            )
            session.commit()
            return build.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: deliver(), range(2)))
    assert ids[0] == ids[1]
    with factory() as session:
        assert session.scalar(select(func.count(CatalogBuild.id))) == 1
        assert session.scalar(select(func.count(CatalogSnapshot.id))) == 1
        assert session.scalar(select(func.count(Job.id))) == 1


def test_create_validation_and_readiness_conflict(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        blocked = Product(name="Blocked", brand=Brand(name="Acme"))
        session.add(blocked)
        inactive = CatalogBrandProfile(
            key="inactive",
            display_name="Inactive",
            primary_color="#111111",
            accent_color="#222222",
            is_active=False,
        )
        session.add(inactive)
        session.commit()

    empty = _request(product, profile)
    empty["product_ids"] = []
    assert client.post("/api/catalog-builder/builds", json=empty).status_code == 422
    unknown = _request(product, profile)
    unknown["product_ids"] = [str(uuid.uuid4())]
    assert client.post("/api/catalog-builder/builds", json=unknown).status_code == 404
    not_ready = _request(product, profile)
    not_ready["product_ids"] = [str(blocked.id)]
    conflict = client.post("/api/catalog-builder/builds", json=not_ready)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "catalog_readiness_changed"
    assert conflict.json()["detail"]["products"][0]["blockers"]
    inactive_request = _request(product, inactive)
    assert client.post("/api/catalog-builder/builds", json=inactive_request).status_code == 409
    missing_profile = _request(product, profile)
    missing_profile["catalog_brand_profile_id"] = str(uuid.uuid4())
    assert client.post("/api/catalog-builder/builds", json=missing_profile).status_code == 404
    bad_layout = _request(product, profile)
    bad_layout["layout_key"] = "unknown"
    assert client.post("/api/catalog-builder/builds", json=bad_layout).status_code == 404
    with factory() as session:
        assert session.scalar(select(func.count(CatalogBuild.id))) == 0
        assert session.scalar(select(func.count(CatalogSnapshot.id))) == 0
        assert session.scalar(select(func.count(Job.id))) == 0


def test_success_status_and_safe_pdf_delivery(build_store):
    factory, client, storage_root, catalogs_dir = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
    created = client.post(
        "/api/catalog-builder/builds", json=_request(product, profile)
    ).json()
    with factory() as session:
        job = session.get(Job, session.get(CatalogBuild, uuid.UUID(created["id"])).job_id)
        job.status = JobStatus.RUNNING
        session.commit()
    assert client.get(f"/api/catalog-builder/builds/{created['id']}").json()["status"] == "running"
    with factory() as session:
        job = session.get(Job, session.get(CatalogBuild, uuid.UUID(created["id"])).job_id)
        job.status = JobStatus.QUEUED
        session.commit()
    _worker(factory, storage_root, catalogs_dir, _valid_pdf()).run_once()
    result = client.get(f"/api/catalog-builder/builds/{created['id']}")
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "succeeded"
    assert body["artifact"]["page_count"] == 1
    assert "file_path" not in str(body)
    artifact_id = body["artifact"]["id"]

    preview = client.get(f"/api/catalog-builder/artifacts/{artifact_id}/pdf")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "application/pdf"
    assert preview.headers["content-disposition"].startswith("inline")
    download = client.get(
        f"/api/catalog-builder/artifacts/{artifact_id}/pdf?download=true"
    )
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.content == preview.content
    assert client.get(f"/api/catalog-builder/artifacts/{uuid.uuid4()}/pdf").status_code == 404

    with factory() as session:
        artifact = session.get(CatalogArtifact, uuid.UUID(artifact_id))
        artifact.file_path = str(storage_root / "outside.pdf")
        session.commit()
    assert client.get(f"/api/catalog-builder/artifacts/{artifact_id}/pdf").status_code == 409


def test_failed_render_retry_reuses_frozen_snapshot_branding_and_layout(build_store):
    factory, client, storage_root, catalogs_dir = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
    created = client.post(
        "/api/catalog-builder/builds", json=_request(product, profile)
    ).json()
    _worker(factory, storage_root, catalogs_dir, b"not a PDF").run_once()
    failed = client.get(f"/api/catalog-builder/builds/{created['id']}").json()
    assert failed["status"] == "failed"
    assert failed["can_retry"] is True
    assert "Traceback" not in failed["error"]

    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(created["id"]))
        snapshot_id = build.catalog_snapshot_id
        job_id = build.job_id
        payload = dict(build.job.payload)
    retried = client.post(f"/api/catalog-builder/builds/{created['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["id"] == created["id"]
    assert retried.json()["status"] == "queued"
    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(created["id"]))
        assert build.catalog_snapshot_id == snapshot_id
        assert build.job_id == job_id
        assert build.job.payload == payload
        assert session.scalar(select(func.count(CatalogSnapshot.id))) == 1
    _worker(factory, storage_root, catalogs_dir, _valid_pdf()).run_once()
    completed = client.get(f"/api/catalog-builder/builds/{created['id']}").json()
    assert completed["status"] == "succeeded"
    assert completed["catalog_snapshot_id"] == str(snapshot_id)
    assert completed["artifact"]["page_count"] == 1
