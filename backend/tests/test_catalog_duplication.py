"""Historical selection is a template; current commerce is built afresh."""

import uuid
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

from PIL import Image

from sqlalchemy import func, select

from app.db import CatalogArtifact, CatalogBuild, CatalogBrandProfile, CatalogRenderRun, CatalogSnapshot, Category, Job, Photo, Price, Product, SKU
from app.domain.enums import JobStatus, PhotoRole
from app.services.catalog_builds import hash_catalog_build_request
from app.domain.schemas import CatalogBuildCreate
from app.domain.schemas import CatalogRenderJobPayloadV2
from app.services.catalog_duplication import get_catalog_duplicate_template
import app.services.catalog_duplication as duplication_service
from app.services.catalog_snapshots import read_catalog_snapshot_data
from app.services.catalog_cover_assets import ingest_catalog_cover_asset
from app.rendering.catalog_layouts import UnknownCatalogLayoutError
from app.rendering.catalog_themes import UnknownCatalogThemeError
from app.services.product_copy_review import resolve_effective_product_copy
from app.services.catalog_rendering import build_catalog_render_idempotency_key
from test_catalog_build_flow import _add_copy, _request, _seed_ready, build_store  # noqa: F401


def _create(client, product, profile, key, **choices):
    response = client.post("/api/catalog-builder/builds", json={**_request(product, profile, key), **choices})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _counts(session):
    return tuple(session.scalar(select(func.count(model.id))) for model in (CatalogBuild, CatalogSnapshot, Job, CatalogRenderRun, CatalogArtifact))


def test_template_is_read_only_and_resolves_current_product_publisher_and_versions(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, price, _, profile = _seed_ready(session, storage_root)
        profile.contact_text = "Historical contact"
        session.commit()
        product_id, profile_id = product.id, profile.id
    source_id = _create(client, product, profile, "duplicate-source-v5", theme_key="premium", theme_version="1",
                        cover={"enabled": True, "cover_key": "editorial", "cover_version": "1", "title": "Septiembre", "edition_label": "2026"},
                        closing={"enabled": True, "closing_key": "order", "closing_version": "1", "heading": "Pedí hoy",
                                 "publisher_contact": {"enabled": True}, "whatsapp": {"enabled": True, "override": "+595 981 111222"},
                                 "qr": {"enabled": True, "target_type": "whatsapp"}})
    with factory() as session:
        source = session.get(CatalogBuild, uuid.UUID(source_id))
        source_snapshot = read_catalog_snapshot_data(session, source.catalog_snapshot_id)
        before = _counts(session)
        source_state = (source.created_at, source.request_hash, source.catalog_snapshot.content_hash, source.job.updated_at)
        session.get(Product, product_id).name = "Premium Whey Pro"
        session.get(Price, price.id).amount = Decimal("390000")
        session.get(CatalogBrandProfile, profile_id).display_name = "Current Publisher"
        session.get(CatalogBrandProfile, profile_id).contact_text = "Current contact"
        session.commit()
        current_state = (
            session.get(Product, product_id).name,
            session.get(Price, price.id).amount,
            session.get(CatalogBrandProfile, profile_id).display_name,
            session.get(CatalogBrandProfile, profile_id).contact_text,
        )
    response = client.get(f"/api/catalogs/{source_id}/duplicate-template")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["can_initialize"] and plan["source"]["historical_publisher_name"] == "Grabelan Natural Market"
    assert plan["products"][0]["status"] == "ready"
    assert plan["products"][0]["current_name"] == "Premium Whey Pro"
    assert plan["publisher"]["current_profile_id"] == str(profile_id)
    assert plan["publisher"]["current_name"] == "Current Publisher"
    assert plan["layout"] == {"state": "copied", "key": "classic", "version": "1"}
    assert plan["theme"] == {"state": "copied", "key": "premium", "version": "1"}
    assert plan["palette"]["source"] == "publisher"
    assert plan["cover"]["title"] == "Septiembre"
    assert plan["closing"]["choice"]["publisher_contact"] == {"enabled": True, "override": None}
    assert plan["closing"]["choice"]["whatsapp"]["override"] == "+595 981 111222"
    assert plan["closing"]["choice"]["qr"]["target_type"] == "whatsapp"
    assert not any(item["code"] == "publisher_contact_provenance_unavailable" for item in plan["warnings"])
    assert client.get(f"/api/catalogs/{source_id}/duplicate-template").json() == plan
    with factory() as session:
        source = session.get(CatalogBuild, uuid.UUID(source_id))
        assert _counts(session) == before
        assert (source.created_at, source.request_hash, source.catalog_snapshot.content_hash, source.job.updated_at) == source_state
        assert read_catalog_snapshot_data(session, source.catalog_snapshot_id) == source_snapshot
        assert (
            session.get(Product, product_id).name,
            session.get(Price, price.id).amount,
            session.get(CatalogBrandProfile, profile_id).display_name,
            session.get(CatalogBrandProfile, profile_id).contact_text,
        ) == current_state

    new_request = {**_request(product, profile, "new-from-source"), "source_build_id": source_id,
                   "theme_key": "premium", "theme_version": "1", "cover": {"enabled": False}, "closing": {"enabled": False}}
    created = client.post("/api/catalog-builder/builds", json=new_request)
    assert created.status_code == 200, created.text
    assert created.json()["source_build_id"] == source_id
    with factory() as session:
        source = session.get(CatalogBuild, uuid.UUID(source_id))
        new = session.get(CatalogBuild, uuid.UUID(created.json()["id"]))
        assert new.catalog_snapshot_id != source.catalog_snapshot_id
        frozen = read_catalog_snapshot_data(session, new.catalog_snapshot_id)
        assert source_snapshot.sections[0].products[0].product_name == "Premium Whey"
        assert frozen.sections[0].products[0].product_name == "Premium Whey Pro"
        assert source_snapshot.sections[0].products[0].variants[0].price.amount == Decimal("350000")
        assert frozen.sections[0].products[0].variants[0].price.amount == Decimal("390000")
        assert new.job.payload["branding_data"]["display_name"] == "Current Publisher"
        assert new.job.payload["closing_data"]["contacts"] == []
    history = client.get(f"/api/catalogs/{created.json()['id']}").json()
    assert history["source_build_id"] == source_id and history["source_build_created_at"]


def test_legacy_defaults_custom_palette_warning_and_invalid_snapshot(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
    ids = [
        _create(client, product, profile, "duplicate-v2"),
        _create(client, product, profile, "duplicate-v3", theme_key="organic", theme_version="1", primary_color_override="#123456"),
        _create(client, product, profile, "duplicate-v4", theme_key="premium", theme_version="1", cover={"enabled": False}),
        _create(client, product, profile, "duplicate-v5", theme_key="premium", theme_version="1", cover={"enabled": False}, closing={"enabled": False}),
    ]
    new_custom = client.get(f"/api/catalogs/{ids[1]}/duplicate-template").json()
    assert new_custom["palette"]["state"] == "copied"
    assert new_custom["palette"]["primary_color_override"] == "#123456"
    assert new_custom["palette"]["accent_color_override"] is None
    with factory() as session:
        # Simulate an existing pre-14B Job whose original Builder intent was
        # never persisted; the resolved visual pair is not enough to infer it.
        source = session.get(CatalogBuild, uuid.UUID(ids[1]))
        source.job.payload = {key: value for key, value in source.job.payload.items() if key != "builder_choice_provenance"}
        session.commit()
    v2, v3, v4, v5 = [client.get(f"/api/catalogs/{build_id}/duplicate-template").json() for build_id in ids]
    assert v2["theme"] == {"state": "defaulted", "key": "minimal", "version": "1"}
    assert {item["code"] for item in v2["defaults_applied"]} == {"source_predates_theme", "source_predates_cover", "source_predates_closing"}
    assert v3["palette"]["state"] == "unresolved" and v3["palette"]["historical_resolved_primary"] == "#123456"
    assert v3["palette"]["primary_color_override"] is None and v3["palette"]["accent_color_override"] is None
    assert {item["code"] for item in v3["defaults_applied"]} == {"source_predates_cover", "source_predates_closing"}
    assert {item["code"] for item in v4["defaults_applied"]} == {"source_predates_closing"}
    assert v5["defaults_applied"] == []
    assert client.get(f"/api/catalogs/{uuid.uuid4()}/duplicate-template").status_code == 404
    with factory() as session:
        bad = session.get(CatalogBuild, uuid.UUID(ids[3]))
        bad.job.status = JobStatus.FAILED
        bad.catalog_snapshot.payload = {"corrupt": True}
        session.commit()
    failed = client.get(f"/api/catalogs/{ids[3]}/duplicate-template").json()
    assert not failed["can_initialize"] and failed["unavailable_reason"] == "invalid_snapshot"
    detail = client.get(f"/api/catalogs/{ids[3]}").json()
    assert not detail["can_duplicate"]


def test_lineage_changes_request_identity_not_render_payload_and_stale_readiness_rejected(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, price, _, profile = _seed_ready(session, storage_root)
    source = _create(client, product, profile, "lineage-source")
    other_source = _create(client, product, profile, "lineage-other-source")
    normal = _request(product, profile, "same-key")
    with_source = {**normal, "source_build_id": source}
    assert hash_catalog_build_request(CatalogBuildCreate.model_validate(normal)) != hash_catalog_build_request(CatalogBuildCreate.model_validate(with_source))
    assert client.post("/api/catalog-builder/builds", json=with_source).status_code == 200
    assert client.post("/api/catalog-builder/builds", json=with_source).status_code == 200
    assert client.post("/api/catalog-builder/builds", json={**with_source, "source_build_id": other_source}).status_code == 409
    assert client.post("/api/catalog-builder/builds", json={**normal, "idempotency_key": "unknown-source", "source_build_id": str(uuid.uuid4())}).status_code == 404
    with factory() as session:
        build = session.scalar(select(CatalogBuild).where(CatalogBuild.idempotency_key == "same-key"))
        assert build.source_build_id == uuid.UUID(source)
        assert "source_build_id" not in build.job.payload
        payload = CatalogRenderJobPayloadV2.model_validate(build.job.payload)
        assert build_catalog_render_idempotency_key(payload, job_type="catalog.render.v2") == build_catalog_render_idempotency_key(
            payload.model_copy(update={"builder_choice_provenance": None}), job_type="catalog.render.v2"
        )
        session.get(Price, price.id).approved = False
        session.commit()
    plan = client.get(f"/api/catalogs/{source}/duplicate-template").json()
    assert plan["products"][0]["status"] == "not_ready"
    assert plan["products"][0]["blockers"]
    assert any(item["code"] == "no_ready_products" for item in plan["warnings"])
    stale_create = client.post("/api/catalog-builder/builds", json={**with_source, "idempotency_key": "stale-readiness"})
    assert stale_create.status_code == 409


def test_new_snapshot_uses_current_category_copy_media_and_sku_rules(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, vanilla, old_price, old_photo, profile = _seed_ready(session, storage_root)
    source_id = _create(client, product, profile, "commerce-source")
    with factory() as session:
        source = session.get(CatalogBuild, uuid.UUID(source_id))
        frozen_source = read_catalog_snapshot_data(session, source.catalog_snapshot_id)
        session.get(Category, frozen_source.sections[0].category.source_category_id).name = "Suplementos Deportivos"
        session.get(Product, product.id).name = "Premium Whey Pro"
        chocolate = SKU(product_id=product.id, external_sku="LANDER-WHEY-CHOC", flavor="Chocolate")
        session.add(chocolate)
        session.flush()
        session.add(Price(sku_id=chocolate.id, amount=Decimal("410000"), currency="PYG",
                          valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), source="human", approved=True))
        image = BytesIO()
        Image.new("RGB", (24, 36), (90, 125, 170)).save(image, format="PNG")
        content = image.getvalue()
        checksum = hashlib.sha256(content).hexdigest()
        path = storage_root / "originals" / checksum[:2] / f"{checksum}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        session.get(Photo, old_photo.id).role = PhotoRole.OTHER
        current_photo = Photo(product_id=product.id, file_path=str(path), checksum_sha256=checksum,
                              original_filename="current.png", mime_type="image/png", file_size_bytes=len(content),
                              width=24, height=36, role=PhotoRole.FRONT, is_original=True)
        session.add(current_photo)
        session.commit()
        _add_copy(session, session.get(Product, product.id), "Descripción actual revisada.")
        session.commit()
        assert resolve_effective_product_copy(session, product.id).short_description == "Descripción actual revisada."
        chocolate_id, current_photo_id = chocolate.id, current_photo.id
    with factory() as session:
        assert resolve_effective_product_copy(session, product.id).short_description == "Descripción actual revisada."
    plan = client.get(f"/api/catalogs/{source_id}/duplicate-template").json()
    assert plan["products"][0]["status"] == "ready"
    first_new = _create(client, product, profile, "commerce-current-one", source_build_id=source_id)
    with factory() as session:
        new = session.get(CatalogBuild, uuid.UUID(first_new))
        frozen_new = read_catalog_snapshot_data(session, new.catalog_snapshot_id)
        assert frozen_source.sections[0].category.name == "Proteínas"
        assert frozen_new.sections[0].category.name == "Suplementos Deportivos"
        assert frozen_source.sections[0].products[0].short_description == "Descripción humana aprobada."
        assert frozen_new.sections[0].products[0].short_description == "Descripción actual revisada."
        assert frozen_source.sections[0].products[0].hero.source_photo_id == old_photo.id
        assert frozen_new.sections[0].products[0].hero.source_photo_id == current_photo_id
        assert {variant.source_sku_id for variant in frozen_source.sections[0].products[0].variants} == {vanilla.id}
        assert {variant.source_sku_id for variant in frozen_new.sections[0].products[0].variants} == {vanilla.id, chocolate_id}
        session.get(Price, old_price.id).approved = False
        session.commit()
    second_new = _create(client, product, profile, "commerce-current-two", source_build_id=source_id)
    with factory() as session:
        newer = session.get(CatalogBuild, uuid.UUID(second_new))
        frozen_newer = read_catalog_snapshot_data(session, newer.catalog_snapshot_id)
        assert {variant.source_sku_id for variant in frozen_newer.sections[0].products[0].variants} == {chocolate_id}
        assert read_catalog_snapshot_data(session, source.catalog_snapshot_id) == frozen_source


def test_hero_reuse_missing_asset_publisher_and_exact_versions(build_store, monkeypatch):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        profile.contact_text = "Old contact"
        hero = ingest_catalog_cover_asset(session, _hero_bytes(), declared_mime_type="image/png", storage_root=storage_root)
        session.commit()
        hero_id = hero.id
    source_id = _create(client, product, profile, "hero-source", theme_key="organic", theme_version="1",
                        cover={"enabled": True, "cover_key": "hero", "cover_version": "1", "title": "Hero edition", "hero_asset_id": str(hero_id)},
                        closing={"enabled": True, "closing_key": "contact", "closing_version": "1", "heading": "Order",
                                 "publisher_contact": {"enabled": True}, "qr": {"enabled": True, "target_type": "custom_url", "custom_url": "https://example.test/order"}})
    with factory() as session:
        plan = get_catalog_duplicate_template(session, uuid.UUID(source_id), storage_root=storage_root)
        assert plan.cover.hero and plan.cover.hero.asset_id == hero_id
        assert plan.closing.choice.qr.custom_url == "https://example.test/order"
        assert plan.cover.title == "Hero edition"
        build = session.get(CatalogBuild, uuid.UUID(source_id))
        build.job.status = JobStatus.RUNNING
        session.get(CatalogBrandProfile, profile.id).is_active = False
        session.get(duplication_service.CatalogCoverAsset, hero_id).file_path = str(storage_root / "missing.png")
        session.commit()
    with factory() as session:
        plan = get_catalog_duplicate_template(session, uuid.UUID(source_id), storage_root=storage_root)
        assert plan.can_initialize and plan.source.status == "running"
        assert plan.publisher.state == "unavailable"
        assert plan.cover.enabled and plan.cover.hero is None and plan.cover.hero_unavailable
        assert {item.code for item in plan.warnings} >= {"publisher_unavailable", "hero_asset_unavailable", "publisher_contact_unavailable"}
    original_layout = duplication_service.resolve_catalog_layout
    original_theme = duplication_service.resolve_catalog_theme_definition
    monkeypatch.setattr(duplication_service, "resolve_catalog_layout", lambda key: (_ for _ in ()).throw(UnknownCatalogLayoutError("unavailable")))
    monkeypatch.setattr(duplication_service, "resolve_catalog_theme_definition", lambda key, version: (_ for _ in ()).throw(UnknownCatalogThemeError("unavailable")))
    with factory() as session:
        plan = get_catalog_duplicate_template(session, uuid.UUID(source_id), storage_root=storage_root)
        assert plan.layout.state == "unavailable" and plan.theme.state == "unavailable"
    monkeypatch.setattr(duplication_service, "resolve_catalog_layout", original_layout)
    monkeypatch.setattr(duplication_service, "resolve_catalog_theme_definition", original_theme)


def _hero_bytes() -> bytes:
    image = BytesIO()
    Image.new("RGB", (40, 30), (70, 120, 95)).save(image, format="PNG")
    return image.getvalue()


def test_unavailable_product_is_never_name_matched(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        product_id, brand_id = product.id, product.brand_id
    source_id = _create(client, product, profile, "unavailable-source")
    engine = factory.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("DELETE FROM products WHERE id = ?", (product_id.hex,))
        connection.commit()
    with factory() as session:
        replacement = Product(brand_id=brand_id, name="Premium Whey")
        session.add(replacement)
        session.commit()
        assert replacement.id != product_id
    plan = client.get(f"/api/catalogs/{source_id}/duplicate-template").json()
    assert plan["can_initialize"] and plan["products"][0]["status"] == "unavailable"
    assert plan["products"][0]["product_id"] == str(product_id)
    assert plan["products"][0]["historical_name"] == "Premium Whey"
    assert plan["products"][0]["current_name"] is None


def test_new_explicit_publisher_contact_override_survives_and_old_missing_provenance_warns(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        profile.contact_text = "Current Publisher contact"
        session.commit()
    source_id = _create(client, product, profile, "explicit-contact", theme_key="premium", theme_version="1",
                        cover={"enabled": False},
                        closing={"enabled": True, "closing_key": "contact", "closing_version": "1",
                                 "heading": "Contact", "publisher_contact": {"enabled": True, "override": "Special line"}})
    plan = client.get(f"/api/catalogs/{source_id}/duplicate-template").json()
    assert plan["closing"]["choice"]["publisher_contact"] == {"enabled": True, "override": "Special line"}
    assert not any(item["code"] == "publisher_contact_provenance_unavailable" for item in plan["warnings"])
    with factory() as session:
        source = session.get(CatalogBuild, uuid.UUID(source_id))
        source.job.payload = {key: value for key, value in source.job.payload.items() if key != "builder_choice_provenance"}
        session.commit()
    legacy = client.get(f"/api/catalogs/{source_id}/duplicate-template").json()
    assert legacy["closing"]["choice"]["publisher_contact"] == {"enabled": True, "override": None}
    assert any(item["code"] == "publisher_contact_provenance_unavailable" for item in legacy["warnings"])


def test_source_currency_does_not_change_current_builder_currency(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, sku, _, _, profile = _seed_ready(session, storage_root)
        session.add(Price(sku_id=sku.id, amount=Decimal("50"), currency="USD",
                          valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), source="human", approved=True))
        session.commit()
    source_id = _create(client, product, profile, "usd-source", currency="USD")
    plan = client.get(f"/api/catalogs/{source_id}/duplicate-template").json()
    assert plan["products"][0]["status"] == "ready"
    assert any(item["code"] == "source_currency_defaulted" for item in plan["warnings"])
