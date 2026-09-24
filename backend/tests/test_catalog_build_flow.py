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
    CatalogRenderRun,
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
from app.services.catalog_cover_assets import ingest_catalog_cover_asset, freeze_catalog_cover_asset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT / "templates" / "grabelan"
AS_OF = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class FakeRenderer:
    def __init__(self, pdf_bytes: bytes):
        self.pdf_bytes = pdf_bytes
        self.html = None

    def render(self, html, config):
        self.html = html
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


def test_themed_build_freezes_palette_and_reuses_action_identity(build_store):
    factory, client, storage_root, catalogs_dir = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        profile_id = profile.id
    themes = client.get("/api/catalog-builder/themes")
    assert themes.status_code == 200
    assert [item["key"] for item in themes.json()] == ["minimal", "premium", "organic", "bold"]
    request = {**_request(product, profile), "theme_key": "premium", "theme_version": "1", "primary_color_override": "#aabbcc"}
    first = client.post("/api/catalog-builder/builds", json=request)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["theme_key"], body["theme_display_label"], body["palette_source"]) == ("premium", "Premium", "custom")
    assert (body["primary_color"], body["accent_color"]) == ("#AABBCC", "#C89B3C")
    assert client.post("/api/catalog-builder/builds", json=request).json()["id"] == body["id"]
    changed = {**request, "theme_key": "organic"}
    assert client.post("/api/catalog-builder/builds", json=changed).status_code == 409
    changed["idempotency_key"] = "new-theme-action"
    other = client.post("/api/catalog-builder/builds", json=changed)
    assert other.status_code == 200 and other.json()["id"] != body["id"]
    invalid = {**request, "theme_version": "2", "idempotency_key": "invalid-version"}
    assert client.post("/api/catalog-builder/builds", json=invalid).status_code == 404
    invalid = {**request, "primary_color_override": "red", "idempotency_key": "invalid-color"}
    assert client.post("/api/catalog-builder/builds", json=invalid).status_code == 422
    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(body["id"]))
        assert build.job.job_type == "catalog.render.v3"
        payload = build.job.payload
        assert payload["theme_data"]["primary_color"] == "#AABBCC"
        assert payload["theme_data"]["accent_color"] == "#C89B3C"
        assert payload["theme_hash"] != session.get(CatalogBuild, uuid.UUID(other.json()["id"])).job.payload["theme_hash"]
        session.get(CatalogBrandProfile, profile_id).primary_color = "#112233"
        session.commit()
    assert client.get(f"/api/catalog-builder/builds/{body['id']}").json()["primary_color"] == "#AABBCC"
    with factory() as session:
        job = session.get(CatalogBuild, uuid.UUID(body["id"])).job
        job.status = JobStatus.FAILED
        job.attempts = 1
        session.commit()
    retried = client.post(f"/api/catalog-builder/builds/{body['id']}/retry")
    assert retried.status_code == 200 and retried.json()["primary_color"] == "#AABBCC"
    renderer = FakeRenderer(_valid_pdf())
    worker = JobWorker(factory, catalog_render_handlers(factory, renderer, storage_root=storage_root,
        catalogs_dir=catalogs_dir, template_root=TEMPLATE_ROOT), accepted_job_types={"catalog.render.v3"})
    worker.run_once()
    assert renderer.html is not None and "theme-premium" in renderer.html and "#AABBCC" in renderer.html
    result = client.get(f"/api/catalog-builder/builds/{body['id']}").json()
    assert result["status"] == "succeeded" and result["artifact"]["page_count"] == 1
    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(body["id"]))
        run = session.scalar(select(CatalogRenderRun).where(CatalogRenderRun.job_id == build.job_id))
        assert run.theme_hash == payload["theme_hash"] and run.theme_data["theme_key"] == "premium"
        assert session.get(CatalogBrandProfile, profile_id).primary_color == "#112233"


def test_v4_cover_build_freezes_hero_edition_and_retry(build_store):
    factory, client, storage_root, catalogs_dir = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        hero_a = ingest_catalog_cover_asset(session, _image_bytes(), declared_mime_type="image/png", storage_root=storage_root)
        output = BytesIO()
        Image.new("RGB", (40, 20), (180, 130, 80)).save(output, format="PNG")
        hero_b = ingest_catalog_cover_asset(session, output.getvalue(), declared_mime_type="image/png", storage_root=storage_root)
        session.commit()
        hero_a_id, hero_b_id = hero_a.id, hero_b.id
        profile_id = profile.id
    layouts = client.get("/api/catalog-builder/cover-layouts")
    assert layouts.status_code == 200
    assert [item["key"] for item in layouts.json()] == ["minimal", "editorial", "hero"]
    request = {**_request(product, profile), "theme_key": "premium", "theme_version": "1",
        "cover": {"enabled": True, "cover_key": "hero", "cover_version": "1", "title": "Edición de prueba",
            "subtitle": "Texto explícito", "edition_label": "2026", "show_publisher_logo": True,
            "hero_asset_id": str(hero_a_id)}}
    created = client.post("/api/catalog-builder/builds", json=request)
    assert created.status_code == 200, created.text
    body = created.json()
    assert (body["cover_display_label"], body["cover_title"], body["cover_edition_label"], body["cover_hero_present"]) == ("Hero", "Edición de prueba", "2026", True)
    assert body["cover_show_publisher_logo"] is False  # no actual logo in frozen Branding
    assert client.post("/api/catalog-builder/builds", json=request).json()["id"] == body["id"]
    for field, value in (("title", "Otro título"), ("hero_asset_id", str(hero_b_id)), ("cover_key", "editorial")):
        changed = {**request, "cover": {**request["cover"], field: value}}
        assert client.post("/api/catalog-builder/builds", json=changed).status_code == 409
    next_request = {**request, "idempotency_key": "new-cover-action", "cover": {**request["cover"], "cover_key": "editorial"}}
    assert client.post("/api/catalog-builder/builds", json=next_request).status_code == 200
    missing = {**request, "idempotency_key": "missing-hero", "cover": {**request["cover"], "hero_asset_id": str(uuid.uuid4())}}
    assert client.post("/api/catalog-builder/builds", json=missing).status_code == 404
    no_image = {**request, "idempotency_key": "no-hero", "cover": {"enabled": True, "cover_key": "hero", "cover_version": "1", "title": "Catálogo"}}
    assert client.post("/api/catalog-builder/builds", json=no_image).status_code == 422
    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(body["id"]))
        assert build.job.job_type == "catalog.render.v4"
        frozen = build.job.payload["cover_data"]
        assert frozen["hero"]["source_cover_asset_id"] == str(hero_a_id)
        original_hash = build.job.payload["cover_hash"]
        session.get(CatalogBrandProfile, profile_id).display_name = "Changed Publisher"
        build.job.status = JobStatus.FAILED
        build.job.attempts = 1
        session.commit()
    assert client.post(f"/api/catalog-builder/builds/{body['id']}/retry").status_code == 200
    renderer = FakeRenderer(_valid_pdf())
    worker = JobWorker(factory, catalog_render_handlers(factory, renderer, storage_root=storage_root,
        catalogs_dir=catalogs_dir, template_root=TEMPLATE_ROOT), accepted_job_types={"catalog.render.v4"})
    worker.run_once()
    assert renderer.html is not None and "Edición de prueba" in renderer.html and "cover-hero" in renderer.html
    assert "Grabelan Natural Market" in renderer.html and "Changed Publisher" not in renderer.html
    restored = client.get(f"/api/catalog-builder/builds/{body['id']}").json()
    assert restored["cover_title"] == "Edición de prueba" and restored["status"] == "succeeded"
    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(body["id"]))
        run = session.scalar(select(CatalogRenderRun).where(CatalogRenderRun.job_id == build.job_id))
        assert run.cover_hash == original_hash and run.cover_data == frozen


def test_cover_upload_and_safe_media_api(build_store, monkeypatch):
    factory, client, storage_root, _ = build_store
    import app.services.catalog_cover_assets as cover_assets_service
    real_ingest = cover_assets_service.ingest_catalog_cover_asset
    real_freeze = cover_assets_service.freeze_catalog_cover_asset
    monkeypatch.setattr(catalog_builder_api, "ingest_catalog_cover_asset", lambda session, content, declared_mime_type: real_ingest(
        session, content, declared_mime_type=declared_mime_type, storage_root=storage_root))
    monkeypatch.setattr(catalog_builder_api, "freeze_catalog_cover_asset", lambda asset: real_freeze(asset, storage_root=storage_root))
    monkeypatch.setattr(catalog_builder_api, "DEFAULT_STORAGE_ROOT", storage_root)
    content = _image_bytes()
    response = client.post("/api/catalog-builder/cover-assets", files={"image": ("hero.png", content, "image/png")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["mime_type"], body["width"], body["height"]) == ("image/png", 24, 36)
    assert "file_path" not in body and "checksum" not in body
    assert client.get(body["preview_url"]).content == content
    duplicate = client.post("/api/catalog-builder/cover-assets", files={"image": ("again.png", content, "image/png")})
    assert duplicate.json()["asset_id"] == body["asset_id"]
    assert client.get(f"/api/catalog-builder/cover-assets/{uuid.uuid4()}/image").status_code == 404
    assert client.post("/api/catalog-builder/cover-assets", files={"image": ("bad.png", b"broken", "image/png")}).status_code == 422
    assert client.post("/api/catalog-builder/cover-assets", files={"image": ("wrong.jpg", content, "image/jpeg")}).status_code == 422
    assert client.post("/api/catalog-builder/cover-assets", files={"image": ("large.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")}).status_code == 413


def test_v4_disabled_cover_is_frozen_without_fabricating_v3_cover(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
    request = {**_request(product, profile), "theme_key": "premium", "theme_version": "1", "cover": {"enabled": False}}
    created = client.post("/api/catalog-builder/builds", json=request)
    assert created.status_code == 200, created.text
    assert created.json()["cover_enabled"] is False and created.json()["cover_display_label"] == "None"
    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(created.json()["id"]))
        assert build.job.job_type == "catalog.render.v4"
        assert build.job.payload["cover_data"]["enabled"] is False
        assert build.job.payload["cover_data"]["title"] is None
    old = {key: value for key, value in request.items() if key != "cover"}
    old["idempotency_key"] = "old-v3-action"
    historical = client.post("/api/catalog-builder/builds", json=old)
    assert historical.status_code == 200 and historical.json()["cover_display_label"] == "None"
    with factory() as session:
        assert session.get(CatalogBuild, uuid.UUID(historical.json()["id"])).job.job_type == "catalog.render.v3"


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
