"""History reads frozen publication records without editing them."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import event, select

import app.services.catalog_history as history_service
from app.db import CatalogArtifact, CatalogBrandProfile, CatalogBuild, CatalogRenderRun, CatalogSnapshot, Category, Job, Price, Product
from app.domain.enums import JobStatus
from test_catalog_build_flow import _request, _seed_ready, _valid_pdf, _worker, build_store  # noqa: F401


def _create(client, product, profile, key, **changes):
    response = client.post("/api/catalog-builder/builds", json={**_request(product, profile, key), **changes})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_empty_pagination_and_unknown_detail(build_store):
    _, client, _, _ = build_store
    body = client.get("/api/catalogs").json()
    assert body == {"items": [], "page": 1, "page_size": 20, "total": 0, "all_total": 0}
    assert client.get(f"/api/catalogs/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/catalogs?page_size=101").status_code == 422


def test_list_filters_sort_and_frozen_values(build_store):
    factory, client, storage_root, _ = build_store
    with factory() as session:
        product, _, price, _, profile = _seed_ready(session, storage_root)
        product_id, price_id, profile_id = product.id, price.id, profile.id
    first = _create(client, product, profile, "history-v2")
    second = _create(client, product, profile, "history-v3", theme_key="premium", theme_version="1")
    third = _create(client, product, profile, "history-v4", theme_key="premium", theme_version="1", cover={"enabled": True, "cover_key": "editorial", "cover_version": "1", "title": "Edición especial", "edition_label": "2026"})
    fourth = _create(client, product, profile, "history-v5", theme_key="premium", theme_version="1", cover={"enabled": False}, closing={"enabled": False})
    statements = []
    engine = factory.kw["bind"]
    def count_query(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        assert client.get("/api/catalogs").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert len(statements) <= 6
    with factory() as session:
        for index, build_id in enumerate((first, second, third, fourth)):
            build = session.get(CatalogBuild, uuid.UUID(build_id))
            build.created_at = datetime(2026, 9, 20 + index, tzinfo=timezone.utc)
        session.get(Product, product_id).name = "Changed live product"
        session.get(Price, price_id).amount = Decimal("999999")
        session.get(CatalogBrandProfile, profile_id).display_name = "Changed live publisher"
        category = session.scalar(select(Category))
        category.name = "Changed live category"
        session.commit()
    page = client.get("/api/catalogs?page_size=2").json()
    assert [row["build_id"] for row in page["items"]] == [fourth, third]
    assert (page["total"], page["all_total"], page["page"]) == (4, 4, 1)
    assert [row["build_id"] for row in client.get("/api/catalogs?page_size=2&page=2").json()["items"]] == [second, first]
    assert page["items"][0]["publisher"]["display_name"] == "Grabelan Natural Market"
    assert page["items"][0]["product_count"] == 1
    assert page["items"][0]["cover"]["state"] == "disabled"
    assert page["items"][0]["closing"]["state"] == "disabled"
    assert page["items"][1]["cover"]["title"] == "Edición especial"
    assert client.get("/api/catalogs?search=  grabelan  ").json()["total"] == 4
    assert client.get("/api/catalogs?search=edición").json()["total"] == 1
    assert client.get("/api/catalogs?search=2026").json()["total"] == 1
    assert client.get("/api/catalogs?search=Changed%20live").json()["total"] == 0
    assert client.get("/api/catalogs?publisher=Grabelan%20Natural%20Market&theme=premium").json()["total"] == 3
    assert client.get("/api/catalogs?theme=organic").json()["total"] == 0
    assert client.get("/api/catalogs?period=today").json()["total"] == 0
    assert client.get("/api/catalogs?status=ready").json()["total"] == 0
    assert client.get("/api/catalogs/options").json() == {"publishers": ["Grabelan Natural Market"], "themes": ["premium"]}
    detail = client.get(f"/api/catalogs/{fourth}").json()
    assert detail["products"][0]["product_name"] == "Premium Whey"
    assert detail["products"][0]["category_name"] == "Proteínas"
    assert detail["products"][0]["variants"][0]["price_amount"] == "350000"
    assert detail["publisher"]["display_name"] == "Grabelan Natural Market"
    assert client.get(f"/api/catalogs/{first}").json()["cover"]["state"] == "unavailable"
    assert client.get(f"/api/catalogs/{first}").json()["theme_key"] is None
    assert client.get(f"/api/catalogs/{second}").json()["cover"]["state"] == "unavailable"
    assert client.get(f"/api/catalogs/{third}").json()["closing"]["state"] == "unavailable"
    with factory() as session:
        assert history_service.list_catalog_history(
            session, period="7d", now=datetime(2026, 9, 25, tzinfo=timezone.utc)
        ).total == 4
        same_time = datetime(2026, 9, 20, tzinfo=timezone.utc)
        session.get(CatalogBuild, uuid.UUID(first)).created_at = same_time
        session.get(CatalogBuild, uuid.UUID(second)).created_at = same_time
        session.commit()
    tail = [row["build_id"] for row in client.get("/api/catalogs").json()["items"][-2:]]
    assert tail == sorted((first, second), reverse=True)


def test_status_artifact_attempts_corruption_and_immutability(build_store, monkeypatch):
    factory, client, storage_root, catalogs_dir = build_store
    monkeypatch.setattr(history_service, "DEFAULT_CATALOGS_DIR", catalogs_dir)
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
    ready_id = _create(client, product, profile, "history-ready")
    failed_id = _create(client, product, profile, "history-failed")
    queued_id = _create(client, product, profile, "history-queued")
    running_id = _create(client, product, profile, "history-running")
    with factory() as session:
        for build_id, status in ((failed_id, JobStatus.FAILED), (running_id, JobStatus.RUNNING)):
            build = session.get(CatalogBuild, uuid.UUID(build_id))
            build.job.status = status
        session.commit()
    # Only the ready Job is eligible for this worker; the queued fixture stays queued.
    with factory() as session:
        session.get(CatalogBuild, uuid.UUID(queued_id)).job.status = JobStatus.RUNNING
        session.commit()
    _worker(factory, storage_root, catalogs_dir, _valid_pdf()).run_once()
    with factory() as session:
        session.get(CatalogBuild, uuid.UUID(queued_id)).job.status = JobStatus.QUEUED
        session.commit()
    ready = client.get(f"/api/catalogs/{ready_id}").json()
    assert ready["status"] == "succeeded" and ready["artifact_available"]
    assert ready["render_attempts"][0]["status"] == "succeeded"
    assert ready["page_count"] == 1
    assert client.get(ready["artifact"]["preview_url"]).status_code == 200
    assert client.get(ready["artifact"]["download_url"]).status_code == 200
    assert client.get(f"/api/catalogs/{failed_id}").json()["status"] == "failed"
    assert client.get(f"/api/catalogs/{failed_id}").json()["can_duplicate"]
    assert client.get(f"/api/catalogs/{failed_id}/duplicate-template").json()["can_initialize"]
    assert client.get(f"/api/catalogs/{running_id}").json()["status"] == "running"
    assert client.get(f"/api/catalogs/{queued_id}").json()["status"] == "queued"
    assert client.get("/api/catalogs?status=active").json()["total"] == 2
    assert client.get("/api/catalogs?status=failed").json()["total"] == 1
    assert client.get("/api/catalogs?status=ready").json()["total"] == 1
    with factory() as session:
        before = {
            "builds": [(row.id, row.created_at, row.request_hash) for row in session.scalars(select(CatalogBuild))],
            "snapshots": [(row.id, row.content_hash) for row in session.scalars(select(CatalogSnapshot))],
            "jobs": [(row.id, row.status, row.updated_at) for row in session.scalars(select(Job))],
            "runs": [(row.id, row.status, row.sanitized_error) for row in session.scalars(select(CatalogRenderRun))],
            "artifacts": [(row.id, row.checksum_sha256) for row in session.scalars(select(CatalogArtifact))],
        }
    client.get("/api/catalogs")
    client.get(f"/api/catalogs/{ready_id}")
    client.get(ready["artifact"]["preview_url"])
    client.get(ready["artifact"]["download_url"])
    with factory() as session:
        after = {
            "builds": [(row.id, row.created_at, row.request_hash) for row in session.scalars(select(CatalogBuild))],
            "snapshots": [(row.id, row.content_hash) for row in session.scalars(select(CatalogSnapshot))],
            "jobs": [(row.id, row.status, row.updated_at) for row in session.scalars(select(Job))],
            "runs": [(row.id, row.status, row.sanitized_error) for row in session.scalars(select(CatalogRenderRun))],
            "artifacts": [(row.id, row.checksum_sha256) for row in session.scalars(select(CatalogArtifact))],
        }
    assert after == before
    with factory() as session:
        build = session.get(CatalogBuild, uuid.UUID(failed_id))
        build.catalog_snapshot.payload = {"corrupt": True}
        session.commit()
    rows = client.get("/api/catalogs").json()["items"]
    bad = next(row for row in rows if row["build_id"] == failed_id)
    assert not bad["historical_data_available"] and bad["product_count"] is None
    assert client.get(f"/api/catalogs/{failed_id}").json()["products"] == []
    with factory() as session:
        artifact = session.get(CatalogArtifact, uuid.UUID(ready["artifact"]["id"]))
        artifact.file_path = str(storage_root / "outside.pdf")
        session.commit()
    broken = client.get(f"/api/catalogs/{ready_id}").json()
    assert not broken["artifact_available"] and broken["artifact"] is None
    assert client.get(f"/api/catalogs/{ready_id}/duplicate-template").json()["can_initialize"]
    with factory() as session:
        session.get(CatalogBuild, uuid.UUID(ready_id)).job.status = JobStatus.FAILED
        session.commit()
    disagreement = client.get(f"/api/catalogs/{ready_id}").json()
    assert disagreement["status"] == "failed"
    assert disagreement["latest_render_status"] == "succeeded"
    assert not disagreement["artifact_available"]


def test_v5_closing_and_retry_timeline_use_frozen_values(build_store, monkeypatch):
    factory, client, storage_root, catalogs_dir = build_store
    monkeypatch.setattr(history_service, "DEFAULT_CATALOGS_DIR", catalogs_dir)
    with factory() as session:
        product, _, _, _, profile = _seed_ready(session, storage_root)
        profile.contact_text = "Atención comercial"
        session.commit()
        profile_id = profile.id
    closing_id = _create(
        client, product, profile, "history-closing", theme_key="premium", theme_version="1",
        cover={"enabled": False},
        closing={"enabled": True, "closing_key": "order", "closing_version": "1",
                 "heading": "Hacé tu pedido", "publisher_contact": {"enabled": True},
                 "whatsapp": {"enabled": True, "override": "+595 981 123456"},
                 "qr": {"enabled": True, "target_type": "whatsapp"}},
    )
    with factory() as session:
        session.get(CatalogBrandProfile, profile_id).contact_text = "Changed live contact"
        session.commit()
    frozen = client.get(f"/api/catalogs/{closing_id}").json()
    assert frozen["closing"]["state"] == "enabled"
    assert frozen["closing"]["heading"] == "Hacé tu pedido"
    assert frozen["closing"]["qr_target_url"] == "https://wa.me/595981123456"
    assert frozen["closing"]["contacts"][0]["value"] == "Atención comercial"

    retry_id = _create(client, product, profile, "history-retry")
    _worker(factory, storage_root, catalogs_dir, b"not a PDF").run_once()
    failed = client.get(f"/api/catalogs/{retry_id}").json()
    assert failed["status"] == "failed" and failed["render_attempts"][0]["status"] == "failed"
    assert "Traceback" not in str(failed)
    assert client.post(f"/api/catalog-builder/builds/{retry_id}/retry").status_code == 200
    _worker(factory, storage_root, catalogs_dir, _valid_pdf()).run_once()
    completed = client.get(f"/api/catalogs/{retry_id}").json()
    assert completed["status"] == "succeeded"
    assert [entry["attempt"] for entry in completed["render_attempts"]] == [2, 1]
    assert [entry["status"] for entry in completed["render_attempts"]] == ["succeeded", "failed"]
    assert completed["render_attempts"][1]["error"]
    with factory() as session:
        artifact = session.get(CatalogArtifact, uuid.UUID(completed["artifact"]["id"]))
        file_path = artifact.file_path
    from pathlib import Path
    Path(file_path).unlink()
    missing = client.get(f"/api/catalogs/{retry_id}").json()
    assert not missing["artifact_available"]
