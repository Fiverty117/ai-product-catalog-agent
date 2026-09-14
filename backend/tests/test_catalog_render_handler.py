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
from app.db import Base, CatalogArtifact, CatalogRenderRun, CatalogSnapshot, Job
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
    enqueue_catalog_render,
)
from app.services.catalog_snapshots import hash_catalog_snapshot_data
from app.workers.catalog_render_handler import CatalogRenderJobHandler
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
