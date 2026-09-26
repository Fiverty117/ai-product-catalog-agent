import base64
import hashlib
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import func, select

from app.ai.image_enhancement import ImageEnhancementResult
from app.ai.vision import VisionExtractionResponse
from app.db import Photo, Product, ProductIntakeItem, ImageEnhancementRun, DerivedImage
from app.domain.schemas import CatalogSnapshotCreate, ProductExtractionResult
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.services.catalog_rendering import (
    build_catalog_render_view_model, normalize_catalog_render_config,
    render_catalog_html, resolve_catalog_template,
)
from app.services.catalog_snapshots import create_catalog_snapshot, read_catalog_snapshot_data
from app.services.image_enhancement import IMAGE_ENHANCEMENT_JOB_TYPE, enqueue_image_enhancement
from app.services.image_processing import (
    PROCESSING_VERSION, normalize_image_for_processing, resolve_photo_for_processing,
    inspect_supported_image, PhotoIntakeError, PhotoStorageIntegrityError,
)
from app.services.product_image_editorial import get_product_image_editorial_summary
from app.services.extraction import PRODUCT_EXTRACTION_JOB_TYPE
from app.workers.extraction_handler import ProductExtractionJobHandler
from app.workers.image_enhancement_handler import ImageEnhancementJobHandler
from app.workers.job_worker import JobWorker
from test_product_intake import store, observation, promotion_body, assert_no_canonical  # noqa: F401


def mpo(*, orientation=None):
    out = BytesIO()
    with Image.new("RGB", (18, 12), "red") as primary, Image.new("RGB", (18, 12), "blue") as secondary:
        options = {}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options["exif"] = exif
        primary.save(out, format="MPO", save_all=True, append_images=[secondary], **options)
    return out.getvalue()


def upload(client, content=None):
    return client.post("/api/product-intake/items", files=[
        ("images", ("IMG_0223.JPEG", content if content is not None else mpo(), "image/jpeg")),
    ])


def assert_jpeg(content, size=(18, 12)):
    with Image.open(BytesIO(content)) as image:
        assert image.format == "JPEG"
        assert image.size == size
        red, green, blue = image.getpixel((size[0] // 2, size[1] // 2))
        assert red > 230 and green < 20 and blue < 20  # Not the second blue frame.
        assert image.getexif().get(274, 1) == 1


def test_upload_truth_primary_exif_preview_and_idempotency(store, monkeypatch):
    client, factory, originals = store
    source = mpo(orientation=6)
    with Image.open(BytesIO(source)) as image:
        assert image.format == "MPO" and image.tell() == 0 and image.n_frames == 2
        assert image.mpinfo[0xB002][0]["Attribute"]["MPType"] == "Baseline MP Primary Image"
    created = upload(client, source)
    assert created.status_code == 201, created.text
    item = created.json()
    assert len(item["photos"]) == 1
    assert item["photos"][0]["original_filename"] == "IMG_0223.JPEG"
    assert item["photos"][0]["mime_type"] == "image/mpo"
    preview = client.get(item["photos"][0]["image_url"])
    assert preview.headers["content-type"] == "image/jpeg"
    assert_jpeg(preview.content, (12, 18))
    assert_no_canonical(factory)
    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        assert photo.mime_type == "image/mpo"
        assert photo.checksum_sha256 == hashlib.sha256(source).hexdigest()
        assert Path(photo.file_path).suffix == ".mpo"
        assert Path(photo.file_path).read_bytes() == source
        assert (photo.width, photo.height) == (18, 12)  # Original dimensions, before transpose.
        first = resolve_photo_for_processing(photo)
        def no_reencode(*args):
            raise AssertionError("cached JPEG must not be regenerated")
        monkeypatch.setattr("app.services.image_processing.normalize_image_for_processing", no_reencode)
        second = resolve_photo_for_processing(photo)
        assert first == second
        assert first.checksum_sha256 == hashlib.sha256(preview.content).hexdigest()
    duplicate = upload(client, source)
    assert duplicate.status_code == 201
    assert len(list(originals.rglob("*.mpo"))) == 1
    normalized = originals.parent / "normalized"
    assert len(list(normalized.rglob("*.jpg"))) == 1
    assert len(list(normalized.rglob("*.json"))) == 1
    assert PROCESSING_VERSION in str(next(normalized.rglob("*.json")))


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_ordinary_formats_keep_exact_bytes_without_processing_artifacts(store, image_format):
    client, factory, originals = store
    out = BytesIO()
    with Image.new("RGB", (18, 12), "red") as image:
        image.save(out, format=image_format)
    source = out.getvalue()
    item = upload(client, source).json()
    assert client.get(item["photos"][0]["image_url"]).content == source
    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        asset = resolve_photo_for_processing(photo)
        assert str(asset.file_path) == photo.file_path
        assert asset.checksum_sha256 == photo.checksum_sha256 == hashlib.sha256(source).hexdigest()
    assert not (originals.parent / "normalized").exists()


@pytest.mark.parametrize("failure", ["truncated_frame", "malformed_index", "empty_index", "unsupported"])
def test_bad_sources_fail_without_rows_or_normalized_orphans(store, failure):
    client, factory, originals = store
    source = mpo()
    if failure == "truncated_frame":
        source = source[:-40]
    elif failure == "malformed_index":
        offset = source.index(b"MPF\0") + 4
        source = source[:offset] + b"broken!!" + source[offset + 8:]
    elif failure == "empty_index":
        # A malformed zero-frame MP index is otherwise opened as a baseline JPEG.
        offset = source.index(b"\x01\xb0\x04\x00\x01\x00\x00\x00") + 8
        source = source[:offset] + b"\0" * 4 + source[offset + 4:]
    else:
        out = BytesIO()
        Image.new("RGB", (4, 4)).save(out, format="GIF")
        source = out.getvalue()
    response = upload(client, source)
    assert response.status_code == 422, response.text
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 0
        assert session.scalar(select(func.count()).select_from(ProductIntakeItem)) == 0
    assert not originals.exists()
    assert not (originals.parent / "normalized").exists()
    assert_no_canonical(factory)


def test_mpo_preserves_pixel_safety_and_processing_boundary(monkeypatch):
    with pytest.raises(PhotoIntakeError, match="unsupported image format: MPO"):
        inspect_supported_image(mpo())
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(PhotoIntakeError, match="decodable"):
        normalize_image_for_processing(mpo())


def test_secondary_mpo_frame_cannot_bypass_pixel_limit(monkeypatch):
    out = BytesIO()
    with Image.new("RGB", (4, 4), "red") as primary, Image.new("RGB", (30, 30), "blue") as secondary:
        primary.save(out, format="MPO", save_all=True, append_images=[secondary])
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(PhotoIntakeError, match="decodable"):
        normalize_image_for_processing(out.getvalue())


def test_mpo_extraction_promotion_enhancement_and_frozen_catalog(store):
    client, factory, originals = store
    source = mpo()
    item = upload(client, source).json()
    photo_id = uuid.UUID(item["photos"][0]["id"])

    class Vision:
        name = "openai"
        def extract(self, request):
            assert len(request.images) == 1
            assert request.images[0].mime_type == "image/jpeg"
            assert request.images[0].content != source
            assert_jpeg(request.images[0].content)
            return VisionExtractionResponse(ProductExtractionResult.model_validate({
                "brand_name": observation("Phone Brand"), "product_name": observation("Phone Product"),
                "flavor": observation("Plain"), "size_value": observation("1"),
                "size_unit": observation("kg"), "servings": observation(None),
            }), {})

    response = client.post(f"/api/product-intake/items/{item['id']}/extractions", headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 202
    JobWorker(factory, {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(factory, Vision())}).run_once()
    assert client.get(f"/api/product-intake/items/{item['id']}").json()["status"] == "review_required"
    from app.services.categories import create_category
    with factory() as session:
        category = create_category(session, name="Test Category")
        session.commit()
        category_id = category.id
    response = client.post(f"/api/product-intake/items/{item['id']}/promotion", json=promotion_body(
        category=str(category_id), prices=[{"intake_sku_index": 0, "amount": "100", "currency": "PYG"}],
    ))
    assert response.status_code == 201, response.text
    product_id = uuid.UUID(response.json()["product_id"])
    assert response.json()["product"]["readiness"]["ready"]
    with factory() as session:
        photos = session.scalars(select(Photo)).all()
        assert len(photos) == 1
        assert photos[0].id == photo_id and photos[0].product_id == product_id and photos[0].sku_id is None
        assert photos[0].mime_type == "image/mpo"
        assert Path(photos[0].file_path).read_bytes() == source
        summary = get_product_image_editorial_summary(session, product_id)
        assert summary.can_generate
        snapshot = create_catalog_snapshot(session, CatalogSnapshotCreate(product_ids=[product_id], currency="PYG"), storage_root=originals.parent)
        data = read_catalog_snapshot_data(session, snapshot.id)
        old_payload, old_hash = snapshot.payload.copy(), snapshot.content_hash
        hero = data.sections[0].products[0].hero
        assert hero.source_photo_id == photo_id
        assert hero.source_original_asset.mime_type == "image/mpo"
        assert hero.source_original_asset.checksum_sha256 == hashlib.sha256(source).hexdigest()
        assert hero.presentation_asset.mime_type == "image/jpeg"
        assert hero.presentation_asset.storage_relative_path.startswith("normalized/")
        assert hero.source_derived_image_id is None
        config = normalize_catalog_render_config()
        view = build_catalog_render_view_model(data, config, storage_root=originals.parent)
        uri = view.sections[0].products[0].image_data_uri
        assert uri.startswith("data:image/jpeg;base64,")
        assert_jpeg(base64.b64decode(uri.split(",", 1)[1]))
        html = render_catalog_html(view, resolve_catalog_template(config.template_key))
        assert len(PdfReader(BytesIO(ChromiumCatalogPdfRenderer().render(html, config).pdf_bytes)).pages) >= 1
        assert snapshot.payload == old_payload and snapshot.content_hash == old_hash
        job = enqueue_image_enhancement(session, source_photo_id=photo_id)
        assert job.payload["source_checksum_sha256"] == photos[0].checksum_sha256
        session.commit()

    assert_jpeg(client.get(summary.original_preview_url).content)
    assert_jpeg(client.get(f"/api/catalog-builder/products/{product_id}/image").content)

    class Enhancement:
        name = "openai"
        def enhance(self, request):
            assert request.source.mime_type == "image/jpeg"
            assert_jpeg(request.source.content)
            out = BytesIO()
            Image.new("RGB", (18, 12), "green").save(out, format="PNG")
            return ImageEnhancementResult(out.getvalue(), "png", {})

    JobWorker(factory, {IMAGE_ENHANCEMENT_JOB_TYPE: ImageEnhancementJobHandler(factory, Enhancement(), processed_dir=originals.parent / "processed")}).run_once()
    with factory() as session:
        run = session.scalar(select(ImageEnhancementRun))
        assert run.source_photo_id == photo_id
        assert run.source_photo.checksum_sha256 == hashlib.sha256(source).hexdigest()
        assert session.scalar(select(DerivedImage)).source_photo_id == photo_id
        assert session.scalar(select(func.count()).select_from(Photo)) == 1
        assert session.get(Photo, photo_id).mime_type == "image/mpo"


def test_cached_representation_integrity_is_checked(store):
    client, factory, _ = store
    item = upload(client).json()
    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        asset = resolve_photo_for_processing(photo)
        asset.file_path.write_bytes(b"corrupt cache")
        with pytest.raises((PhotoStorageIntegrityError, PhotoIntakeError)):
            resolve_photo_for_processing(photo)
    assert client.get(item["photos"][0]["image_url"]).status_code == 404


def test_builder_does_not_fall_back_to_serving_raw_mpo_on_cache_failure(store):
    client, factory, _ = store
    item = upload(client).json()
    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        from app.db import Brand
        from app.domain.enums import PhotoRole
        photo.product = Product(name="Test Product", brand=Brand(name="Test Brand"))
        photo.role = PhotoRole.FRONT
        asset = resolve_photo_for_processing(photo)
        asset.file_path.write_bytes(b"broken cache")
        session.commit()
        product_id = photo.product_id
    assert client.get(f"/api/catalog-builder/products/{product_id}/image").status_code == 404


def test_intake_limits_still_apply_before_normalized_storage(store):
    client, factory, originals = store
    too_many = client.post("/api/product-intake/items", files=[
        ("images", (f"phone-{index}.JPEG", mpo(), "image/jpeg")) for index in range(13)
    ])
    assert too_many.status_code == 422
    too_large = upload(client, mpo() + b"x" * (20 * 1024 * 1024))
    assert too_large.status_code == 422
    assert not originals.exists()
    assert not (originals.parent / "normalized").exists()
