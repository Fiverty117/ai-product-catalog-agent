import base64
import hashlib
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import app.workers.catalog_render_handler as handler_module
from app.db import Base, CatalogArtifact, CatalogBrandProfile, CatalogRenderRun, CatalogSnapshot, Job
from app.db.session import create_sqlite_engine
from app.domain.enums import (
    CatalogHeroPhotoSource,
    ExtractionRunStatus,
    JobStatus,
    PhotoPresentationAssetType,
)
from app.domain.schemas import (
    CatalogHeroSnapshot,
    CatalogProductSnapshot,
    CatalogRenderConfig,
    CatalogRenderJobPayloadV2,
    CatalogSectionSnapshot,
    CatalogSnapshotData,
    CatalogVariantSnapshot,
    FrozenCatalogAsset,
    FrozenCatalogCategory,
    FrozenCatalogPrice,
)
from app.rendering.catalog_pdf import (
    CatalogPdfRenderResult,
    RetryableCatalogRendererError,
)
from app.services.catalog_rendering import (
    CATALOG_RENDER_JOB_TYPE,
    CATALOG_RENDER_JOB_TYPE_V2,
    build_catalog_render_idempotency_key,
    enqueue_catalog_render,
    enqueue_catalog_render_v2,
)
from app.services.catalog_branding import (
    create_catalog_brand_profile,
    ingest_catalog_brand_logo,
    update_catalog_brand_profile,
)
from app.services.catalog_snapshots import hash_catalog_snapshot_data
from app.workers.catalog_render_handler import CatalogRenderJobHandler, catalog_render_handlers
from app.workers.job_worker import JobWorker

AS_OF = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


class FixedClock:
    def __init__(self):
        self.current = AS_OF

    def __call__(self):
        return self.current


class FakeRenderer:
    def __init__(self, *outcomes, observer=None):
        self.outcomes = list(outcomes)
        self.observer = observer
        self.calls = []

    def render(self, html, config):
        self.calls.append((html, config))
        if self.observer:
            self.observer()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return CatalogPdfRenderResult(
            pdf_bytes=outcome,
            engine="chromium",
            engine_version="123.4",
        )


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'handler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage_root = tmp_path / "storage"
    catalogs_dir = storage_root / "catalogs"
    yield factory, storage_root, catalogs_dir
    engine.dispose()


def valid_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def create_snapshot(factory, storage_root):
    image_output = BytesIO()
    Image.new("RGB", (13, 11), (10, 20, 30)).save(image_output, format="PNG")
    image = image_output.getvalue()
    checksum = hashlib.sha256(image).hexdigest()
    relative = f"originals/{checksum[:2]}/{checksum}.png"
    path = storage_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    asset = FrozenCatalogAsset(
        checksum_sha256=checksum,
        mime_type="image/png",
        file_size_bytes=len(image),
        width=13,
        height=11,
        storage_relative_path=relative,
    )
    snapshot_data = CatalogSnapshotData(
        schema_version="catalog-snapshot-v1",
        currency="PYG",
        as_of=AS_OF,
        sections=[
            CatalogSectionSnapshot(
                category=FrozenCatalogCategory(
                    source_category_id=uuid.uuid4(),
                    name="Supplements",
                    identity_key="supplements",
                    sort_order=10,
                ),
                products=[
                    CatalogProductSnapshot(
                        source_brand_id=uuid.uuid4(),
                        brand_name="Frozen Brand",
                        brand_identity_key="frozen brand",
                        source_product_id=uuid.uuid4(),
                        product_name="Frozen Product",
                        product_identity_key="frozen product",
                        hero=CatalogHeroSnapshot(
                            source_photo_id=uuid.uuid4(),
                            source_photo_owner_type=CatalogHeroPhotoSource.PRODUCT,
                            source_photo_owner_sku_id=None,
                            source_original_asset=asset,
                            presentation_type=PhotoPresentationAssetType.ORIGINAL,
                            source_derived_image_id=None,
                            presentation_asset=asset,
                        ),
                        variants=[
                            CatalogVariantSnapshot(
                                source_sku_id=uuid.uuid4(),
                                external_sku=None,
                                flavor="Vanilla",
                                size_value=None,
                                size_unit=None,
                                servings=None,
                                price=FrozenCatalogPrice(
                                    source_price_id=uuid.uuid4(),
                                    amount="180000.0000",
                                    currency="PYG",
                                    valid_from=AS_OF,
                                    created_at=AS_OF,
                                    source="manual",
                                ),
                            )
                        ],
                    )
                ],
            )
        ],
    )
    with factory() as session:
        snapshot = CatalogSnapshot(
            schema_version=snapshot_data.schema_version,
            currency=snapshot_data.currency,
            as_of=snapshot_data.as_of,
            payload=snapshot_data.model_dump(mode="json"),
            content_hash=hash_catalog_snapshot_data(snapshot_data),
        )
        session.add(snapshot)
        session.commit()
        return snapshot.id, path


def enqueue(factory, snapshot_id):
    with factory() as session:
        job = enqueue_catalog_render(
            session,
            catalog_snapshot_id=snapshot_id,
            template_root=TEMPLATE_ROOT,
        )
        session.commit()
        return job.id


def make_worker(factory, storage_root, catalogs_dir, renderer, clock=None):
    return JobWorker(
        factory,
        {
            CATALOG_RENDER_JOB_TYPE: CatalogRenderJobHandler(
                factory,
                renderer,
                storage_root=storage_root,
                catalogs_dir=catalogs_dir,
                template_root=TEMPLATE_ROOT,
            )
        },
        **({"clock": clock} if clock else {}),
    )


def test_success_commits_run_before_browser_and_creates_immutable_artifact(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    job_id = enqueue(factory, snapshot_id)

    def observe():
        with factory() as session:
            assert session.get(Job, job_id).status is JobStatus.RUNNING
            run = session.scalar(select(CatalogRenderRun))
            assert run.status is ExtractionRunStatus.RUNNING
            assert session.scalar(select(CatalogArtifact)) is None

    renderer = FakeRenderer(valid_pdf(), observer=observe)
    make_worker(factory, storage_root, catalogs_dir, renderer).run_once()

    with factory() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(CatalogRenderRun))
        artifact = session.scalar(select(CatalogArtifact))
        snapshot = session.get(CatalogSnapshot, snapshot_id)
        assert job.status is JobStatus.SUCCEEDED
        assert run.status is ExtractionRunStatus.SUCCEEDED
        assert run.renderer_engine == "chromium"
        assert run.renderer_engine_version == "123.4"
        assert run.catalog_brand_profile_id is None and run.branding_data is None
        assert artifact.catalog_snapshot_id == snapshot.id
        assert artifact.render_run_id == run.id
        assert artifact.media_type == "application/pdf"
        assert artifact.page_count == 1
        assert artifact.checksum_sha256 == hashlib.sha256(valid_pdf()).hexdigest()
        assert Path(artifact.file_path).is_relative_to(catalogs_dir.resolve())
        assert snapshot.payload["sections"][0]["products"][0]["product_name"] == (
            "Frozen Product"
        )
    html, _ = renderer.calls[0]
    assert "Frozen Product" in html and "180.000" in html


def test_renderer_failure_retries_with_distinct_historical_run(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    job_id = enqueue(factory, snapshot_id)
    renderer = FakeRenderer(
        RetryableCatalogRendererError("temporary crash"), valid_pdf()
    )
    clock = FixedClock()
    worker = make_worker(factory, storage_root, catalogs_dir, renderer, clock)

    worker.run_once()
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.status is JobStatus.QUEUED
        clock.current = job.next_retry_at
    worker.run_once()

    with factory() as session:
        runs = list(
            session.scalars(
                select(CatalogRenderRun).order_by(
                    CatalogRenderRun.created_at, CatalogRenderRun.id
                )
            )
        )
        assert [run.status for run in runs] == [
            ExtractionRunStatus.FAILED,
            ExtractionRunStatus.SUCCEEDED,
        ]
        assert runs[0].id != runs[1].id
        assert session.get(Job, job_id).status is JobStatus.SUCCEEDED
        assert session.scalar(select(CatalogArtifact)).render_run_id == runs[1].id


def test_missing_frozen_asset_is_permanent_and_creates_no_artifact(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, path = create_snapshot(factory, storage_root)
    job_id = enqueue(factory, snapshot_id)
    path.unlink()
    renderer = FakeRenderer(valid_pdf())

    make_worker(factory, storage_root, catalogs_dir, renderer).run_once()

    with factory() as session:
        assert session.get(Job, job_id).status is JobStatus.FAILED
        assert session.scalar(select(CatalogRenderRun)).status is (
            ExtractionRunStatus.FAILED
        )
        assert session.scalar(select(CatalogArtifact)) is None
        assert session.get(CatalogSnapshot, snapshot_id) is not None
    assert renderer.calls == []


def test_invalid_pdf_output_permanently_fails_without_artifact(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    job_id = enqueue(factory, snapshot_id)

    make_worker(
        factory, storage_root, catalogs_dir, FakeRenderer(b"not a PDF")
    ).run_once()

    with factory() as session:
        assert session.get(Job, job_id).status is JobStatus.FAILED
        assert session.scalar(select(CatalogRenderRun)).status is (
            ExtractionRunStatus.FAILED
        )
        assert session.scalar(select(CatalogArtifact)) is None


def test_corrupted_snapshot_fails_safely_before_run(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    job_id = enqueue(factory, snapshot_id)
    with factory() as session:
        session.get(CatalogSnapshot, snapshot_id).content_hash = "0" * 64
        session.commit()

    make_worker(
        factory, storage_root, catalogs_dir, FakeRenderer(valid_pdf())
    ).run_once()

    with factory() as session:
        assert session.get(Job, job_id).status is JobStatus.FAILED
        assert session.scalar(select(CatalogRenderRun)) is None
        assert session.scalar(select(CatalogArtifact)) is None


def test_database_completion_failure_leaves_failed_run_and_retryable_job(
    store, monkeypatch
):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    job_id = enqueue(factory, snapshot_id)

    def fail_completion(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(handler_module, "complete_catalog_render_run", fail_completion)
    make_worker(
        factory, storage_root, catalogs_dir, FakeRenderer(valid_pdf())
    ).run_once()

    with factory() as session:
        assert session.get(Job, job_id).status is JobStatus.QUEUED
        assert session.scalar(select(CatalogRenderRun)).status is (
            ExtractionRunStatus.FAILED
        )
        assert session.scalar(select(CatalogArtifact)) is None
    assert len(list(catalogs_dir.rglob("*.pdf"))) == 1


def _create_publisher(factory, storage_root, key="grabelan", logo=None):
    with factory() as session:
        asset = ingest_catalog_brand_logo(session, logo, storage_root=storage_root) if logo else None
        profile = create_catalog_brand_profile(session, {
            "key": key, "display_name": "Publisher A", "primary_color": "#112233",
            "accent_color": "#AABBCC",
        }, logo_asset=asset, storage_root=storage_root)
        session.commit()
        return profile.id, asset.id if asset else None


def _enqueue_v2(factory, storage_root, snapshot_id, profile_id, *, layout="classic"):
    with factory() as session:
        job = enqueue_catalog_render_v2(
            session, catalog_snapshot_id=snapshot_id, brand_profile_id=profile_id,
            config=CatalogRenderConfig(layout=layout),
            storage_root=storage_root, template_root=TEMPLATE_ROOT,
        )
        session.commit()
        return job.id, job.idempotency_key, job.payload


def _v2_worker(factory, storage_root, catalogs_dir, renderer):
    return JobWorker(factory, catalog_render_handlers(
        factory, renderer, storage_root=storage_root, catalogs_dir=catalogs_dir,
        template_root=TEMPLATE_ROOT,
    ))


def test_v2_freezes_branding_at_enqueue_and_does_not_hold_transaction_during_browser(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    profile_id, _ = _create_publisher(factory, storage_root)
    job_id, _, original_payload = _enqueue_v2(factory, storage_root, snapshot_id, profile_id)
    assert original_payload["branding_data"]["display_name"] == "Publisher A"
    with factory() as session:
        profile = session.get(CatalogBrandProfile, profile_id)
        update_catalog_brand_profile(session, profile, {"display_name": "Publisher B", "accent_color": "#334455"})
        session.commit()

    def observe():
        with factory() as session:
            run = session.scalar(select(CatalogRenderRun))
            assert run.status is ExtractionRunStatus.RUNNING
            assert run.branding_data["display_name"] == "Publisher A"
            profile = session.get(CatalogBrandProfile, profile_id)
            update_catalog_brand_profile(session, profile, {"display_name": "Publisher C"})
            session.commit()  # Slow browser boundary holds no SQLite transaction.

    renderer = FakeRenderer(valid_pdf(), observer=observe)
    _v2_worker(factory, storage_root, catalogs_dir, renderer).run_once()
    with factory() as session:
        run = session.scalar(select(CatalogRenderRun))
        assert session.get(Job, job_id).status is JobStatus.SUCCEEDED
        assert run.catalog_brand_profile_id == profile_id
        assert run.branding_schema_version == "catalog-branding-v1"
        assert run.branding_hash == original_payload["branding_hash"]
        assert run.branding_data == original_payload["branding_data"]
        assert run.layout_key == "classic"
        assert run.layout_version == "1"
        assert session.get(CatalogBrandProfile, profile_id).display_name == "Publisher C"
    html, _ = renderer.calls[0]
    assert "Publisher A" in html and "Publisher B" not in html and "Publisher C" not in html
    assert 'class="store-name"' in html and 'class="publisher-logo"' not in html
    assert "Frozen Brand" in html  # Product Brand remains snapshot commerce content.


def test_v2_idempotency_uses_profile_lineage_and_visual_branding_hash(store):
    factory, storage_root, _ = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    first_id, _ = _create_publisher(factory, storage_root, "grabelan")
    second_id, _ = _create_publisher(factory, storage_root, "gravefit")
    first_job, first_key, first_payload = _enqueue_v2(factory, storage_root, snapshot_id, first_id)
    same_job, same_key, _ = _enqueue_v2(factory, storage_root, snapshot_id, first_id)
    _, second_key, second_payload = _enqueue_v2(factory, storage_root, snapshot_id, second_id)
    assert first_job == same_job and first_key == same_key
    assert first_payload["branding_hash"] == second_payload["branding_hash"]
    assert first_key != second_key
    with factory() as session:
        update_catalog_brand_profile(session, session.get(CatalogBrandProfile, first_id), {"primary_color": "#010203"})
        session.commit()
    _, changed_key, changed_payload = _enqueue_v2(factory, storage_root, snapshot_id, first_id)
    assert changed_payload["branding_hash"] != first_payload["branding_hash"]
    assert changed_key != first_key
    with factory() as session:
        assert session.get(Job, first_job).job_type == CATALOG_RENDER_JOB_TYPE_V2


def test_v2_layout_is_explicit_in_idempotency_and_successful_run_audit(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    profile_id, _ = _create_publisher(factory, storage_root)

    dense_id, dense_key, dense_data = _enqueue_v2(
        factory, storage_root, snapshot_id, profile_id, layout="dense"
    )
    classic_id, classic_key, classic_data = _enqueue_v2(
        factory, storage_root, snapshot_id, profile_id, layout="classic"
    )
    same_id, same_key, _ = _enqueue_v2(
        factory, storage_root, snapshot_id, profile_id, layout="classic"
    )
    compact_id, compact_key, compact_data = _enqueue_v2(
        factory, storage_root, snapshot_id, profile_id, layout="compact"
    )

    assert (classic_id, classic_key) == (same_id, same_key)
    assert len({classic_id, dense_id, compact_id}) == 3
    assert len({classic_key, dense_key, compact_key}) == 3
    assert [(data["layout_key"], data["layout_version"]) for data in (classic_data, dense_data, compact_data)] == [
        ("classic", "1"),
        ("dense", "1"),
        ("compact", "1"),
    ]

    future_dense = CatalogRenderJobPayloadV2.model_validate(dense_data).model_copy(
        update={"layout_version": "2"}
    )
    assert build_catalog_render_idempotency_key(
        future_dense, job_type=CATALOG_RENDER_JOB_TYPE_V2
    ) != dense_key

    renderer = FakeRenderer(valid_pdf())
    _v2_worker(factory, storage_root, catalogs_dir, renderer).run_once()
    with factory() as session:
        dense_run = session.scalar(
            select(CatalogRenderRun).where(CatalogRenderRun.job_id == dense_id)
        )
        assert dense_run.layout_key == "dense"
        assert dense_run.layout_version == "1"
    assert 'class="layout layout-dense"' in renderer.calls[0][0]


def test_v2_logo_swap_does_not_change_queued_job_and_missing_frozen_logo_fails(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    logo_output = BytesIO()
    Image.new("RGB", (17, 14), (80, 90, 100)).save(logo_output, format="PNG")
    logo_a = logo_output.getvalue()
    profile_id, asset_id = _create_publisher(factory, storage_root, logo=logo_a)
    job_id, _, frozen_payload = _enqueue_v2(factory, storage_root, snapshot_id, profile_id)
    logo_output = BytesIO()
    Image.new("RGB", (17, 14), (100, 90, 80)).save(logo_output, format="PNG")
    with factory() as session:
        newer = ingest_catalog_brand_logo(session, logo_output.getvalue(), storage_root=storage_root)
        update_catalog_brand_profile(session, session.get(CatalogBrandProfile, profile_id), {}, logo_asset=newer, change_logo=True, storage_root=storage_root)
        session.commit()
    frozen_path = storage_root / frozen_payload["branding_data"]["logo"]["storage_relative_path"]
    frozen_path.unlink()
    renderer = FakeRenderer(valid_pdf())
    _v2_worker(factory, storage_root, catalogs_dir, renderer).run_once()
    with factory() as session:
        assert session.get(Job, job_id).status is JobStatus.FAILED
        run = session.scalar(select(CatalogRenderRun))
        assert run.branding_data["logo"]["source_brand_asset_id"] == str(asset_id)
        assert run.status is ExtractionRunStatus.FAILED
        assert session.scalar(select(CatalogArtifact)) is None
    assert renderer.calls == []


def test_v2_valid_logo_is_embedded_offline_and_historical_run_survives_deactivation(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    output = BytesIO()
    Image.new("RGB", (18, 12), (38, 68, 98)).save(output, format="PNG")
    profile_id, _ = _create_publisher(factory, storage_root, logo=output.getvalue())
    job_id, _, frozen = _enqueue_v2(factory, storage_root, snapshot_id, profile_id)
    renderer = FakeRenderer(valid_pdf())
    newer_output = BytesIO()
    Image.new("RGB", (18, 12), (98, 68, 38)).save(newer_output, format="PNG")
    with factory() as session:
        newer = ingest_catalog_brand_logo(session, newer_output.getvalue(), storage_root=storage_root)
        update_catalog_brand_profile(session, session.get(CatalogBrandProfile, profile_id), {}, logo_asset=newer, change_logo=True, storage_root=storage_root)
        session.commit()
    _v2_worker(factory, storage_root, catalogs_dir, renderer).run_once()
    html, _ = renderer.calls[0]
    assert 'class="publisher-logo"' in html
    assert "data:image/png;base64," in html
    assert base64.b64encode(output.getvalue()).decode("ascii") in html
    assert base64.b64encode(newer_output.getvalue()).decode("ascii") not in html
    assert hashlib.sha256(output.getvalue()).hexdigest() == frozen["branding_data"]["logo"]["checksum_sha256"]
    assert newer_output.getvalue() != output.getvalue()
    assert frozen["branding_data"]["logo"]["checksum_sha256"] != hashlib.sha256(newer_output.getvalue()).hexdigest()
    assert "https://" not in html and "file://" not in html
    with factory() as session:
        update_catalog_brand_profile(session, session.get(CatalogBrandProfile, profile_id), {"is_active": False})
        session.commit()
    with factory() as session:
        run = session.scalar(select(CatalogRenderRun))
        assert session.get(Job, job_id).status is JobStatus.SUCCEEDED
        assert run.branding_data == frozen["branding_data"]
        assert session.scalar(select(CatalogArtifact)) is not None
        with pytest.raises(Exception, match="inactive"):
            enqueue_catalog_render_v2(session, catalog_snapshot_id=snapshot_id,
                                      brand_profile_id=profile_id, storage_root=storage_root,
                                      template_root=TEMPLATE_ROOT)


def test_v2_malformed_frozen_hash_permanently_fails_before_render_run(store):
    factory, storage_root, catalogs_dir = store
    snapshot_id, _ = create_snapshot(factory, storage_root)
    profile_id, _ = _create_publisher(factory, storage_root)
    job_id, _, _ = _enqueue_v2(factory, storage_root, snapshot_id, profile_id)
    with factory() as session:
        job = session.get(Job, job_id)
        job.payload = {**job.payload, "branding_hash": "0" * 64}
        session.commit()
    renderer = FakeRenderer(valid_pdf())
    _v2_worker(factory, storage_root, catalogs_dir, renderer).run_once()
    with factory() as session:
        assert session.get(Job, job_id).status is JobStatus.FAILED
        assert session.scalar(select(CatalogRenderRun)) is None
        assert session.scalar(select(CatalogArtifact)) is None
    assert renderer.calls == []
