import base64
import hashlib
import json
import uuid
import warnings
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageOps, PngImagePlugin
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
    LEGACY_MPO_PROCESSING_VERSION, LEGACY_PROCESSING_VERSION,
    PROCESSING_VERSION,
    normalize_image_for_processing, resolve_photo_for_processing,
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


def asymmetric_image(*, left=(255, 0, 0), right=(0, 0, 255)):
    image = Image.new("RGB", (40, 24), left)
    image.paste(right, (20, 0, 40, 24))
    return image


def asymmetric_jpeg(*, orientation=None, left=(255, 0, 0), right=(0, 0, 255)):
    out = BytesIO()
    with asymmetric_image(left=left, right=right) as image:
        options = {"quality": 100, "subsampling": 0}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options["exif"] = exif
        image.save(out, format="JPEG", **options)
    return out.getvalue()


def asymmetric_oriented_image(image_format):
    out = BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    options = {"exif": exif}
    if image_format == "WEBP":
        options["lossless"] = True
    with asymmetric_image() as image:
        image.save(out, format=image_format, **options)
    return out.getvalue()


def xmp_only_oriented_png():
    out = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_itxt(
        "XML:com.adobe.xmp",
        (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description xmlns:tiff="http://ns.adobe.com/tiff/1.0/">'
            "<tiff:Orientation>6</tiff:Orientation>"
            "</rdf:Description></rdf:RDF></x:xmpmeta>"
        ),
    )
    with Image.new("RGBA", (24, 40), (255, 0, 0, 255)) as image:
        image.paste((0, 0, 255, 255), (0, 20, 24, 40))
        image.save(out, format="PNG", pnginfo=metadata)
    return out.getvalue()


def asymmetric_mpo(*, orientation=6):
    out = BytesIO()
    with asymmetric_image() as primary, Image.new("RGB", (40, 24), "green") as secondary:
        exif = Image.Exif()
        exif[274] = orientation
        primary.save(
            out,
            format="MPO",
            save_all=True,
            append_images=[secondary],
            exif=exif,
            quality=100,
            subsampling=0,
        )
    return out.getvalue()


def mpo_with_invalid_auxiliary(*, orientation=None, asymmetric=False):
    source = (
        asymmetric_mpo(orientation=orientation)
        if asymmetric
        else mpo(orientation=orientation)
    )
    corrupted = bytearray(source)
    with Image.open(BytesIO(source)) as image:
        assert image.format == "MPO" and image.n_frames == 2
        image.seek(1)
        auxiliary_offset = image.offset
    corrupted[auxiliary_offset : auxiliary_offset + 4] = b"BAD!"
    return bytes(corrupted)


def mpo_with_invalid_primary():
    source = mpo()
    corrupted = bytearray(source)
    with Image.open(BytesIO(source)) as image:
        primary_size = image.mpinfo[0xB002][0]["Size"]
    end_marker = source.rfind(b"\xff\xd9", 0, primary_size)
    assert end_marker > 0
    corrupted[end_marker : end_marker + 2] = b"ZZ"
    return bytes(corrupted)


def mpo_with_stale_mpf_metadata():
    source = asymmetric_mpo(orientation=3)
    rewritten = bytearray(source)
    with Image.open(BytesIO(source)) as image:
        entries = image.mpinfo[0xB002]
    mpf_base = source.index(b"MPF\0") + 4
    byte_order = {b"II": "little", b"MM": "big"}[source[mpf_base : mpf_base + 2]]
    replacements = (
        (len(source) + 2520, 0),
        (83424, len(source) - 16 - mpf_base),
    )
    for entry, (new_size, new_data_offset) in zip(
        entries,
        replacements,
        strict=True,
    ):
        current_fields = entry["Size"].to_bytes(4, byte_order) + entry[
            "DataOffset"
        ].to_bytes(4, byte_order)
        position = bytes(rewritten).find(current_fields, mpf_base)
        assert position >= mpf_base
        rewritten[position : position + 8] = new_size.to_bytes(
            4, byte_order
        ) + new_data_offset.to_bytes(4, byte_order)
    return bytes(rewritten)


def malformed_mpf_jpeg(kind):
    source = asymmetric_mpo()
    if kind == "malformed_index":
        offset = source.index(b"MPF\0") + 4
        return source[:offset] + b"broken!!" + source[offset + 8:]
    if kind == "empty_index":
        offset = source.index(b"\x01\xb0\x04\x00\x01\x00\x00\x00") + 8
        return source[:offset] + b"\0" * 4 + source[offset + 4:]
    raise AssertionError(f"unknown malformed MPF fixture: {kind}")


def upload(client, content=None, *, filename="IMG_0223.JPEG", mime_type="image/jpeg"):
    return client.post("/api/product-intake/items", files=[
        (
            "images",
            (filename, content if content is not None else mpo(), mime_type),
        ),
    ])


def assert_jpeg(content, size=(18, 12)):
    with Image.open(BytesIO(content)) as image:
        assert image.format == "JPEG"
        assert image.size == size
        red, green, blue = image.getpixel((size[0] // 2, size[1] // 2))
        assert red > 230 and green < 20 and blue < 20  # Not the second blue frame.
        assert image.getexif().get(274, 1) == 1


def assert_upright_jpeg(
    content,
    *,
    top=(255, 0, 0),
    bottom=(0, 0, 255),
):
    with Image.open(BytesIO(content)) as image:
        assert image.format == "JPEG"
        image.load()
        assert image.size == (24, 40)
        assert image.getexif().get(274, 1) == 1
        assert "mp" not in image.info
        actual_top = image.getpixel((12, 8))
        actual_bottom = image.getpixel((12, 32))
    assert max(abs(actual - expected) for actual, expected in zip(actual_top, top)) < 30
    assert max(abs(actual - expected) for actual, expected in zip(actual_bottom, bottom)) < 30


def assert_upright_image(content, image_format):
    with Image.open(BytesIO(content)) as image:
        assert image.format == image_format
        image.load()
        assert image.size == (24, 40)
        assert image.getexif().get(274, 1) == 1
        actual_top = image.getpixel((12, 8))
        actual_bottom = image.getpixel((12, 32))
    assert max(abs(actual - expected) for actual, expected in zip(actual_top, (255, 0, 0))) < 30
    assert max(abs(actual - expected) for actual, expected in zip(actual_bottom, (0, 0, 255))) < 30


def assert_orientation_3_jpeg(content):
    with Image.open(BytesIO(content)) as image:
        assert image.format == "JPEG"
        image.load()
        assert image.size == (40, 24)
        assert image.getexif().get(274, 1) == 1
        assert "mp" not in image.info
        actual_left = image.getpixel((8, 12))
        actual_right = image.getpixel((32, 12))
    assert max(
        abs(actual - expected)
        for actual, expected in zip(actual_left, (0, 0, 255))
    ) < 30
    assert max(
        abs(actual - expected)
        for actual, expected in zip(actual_right, (255, 0, 0))
    ) < 30


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


def test_valid_primary_with_invalid_auxiliary_is_accepted_and_oriented(
    store,
    monkeypatch,
):
    client, factory, originals = store
    diagnostics = []

    def capture_diagnostic(message, *args):
        diagnostics.append(message % args)

    monkeypatch.setattr(
        "app.services.image_processing.logger.warning",
        capture_diagnostic,
    )
    source = mpo_with_invalid_auxiliary(orientation=3, asymmetric=True)
    source_checksum = hashlib.sha256(source).hexdigest()
    with Image.open(BytesIO(source)) as image:
        assert image.format == "MPO" and image.n_frames == 2
        assert image.tell() == 0 and image.getexif().get(274) == 3
        image.load()
        with pytest.raises(SyntaxError, match="not a JPEG file"):
            image.seek(1)

    created = upload(client, source, filename="IMG_0258.JPEG")

    assert created.status_code == 201, created.text
    item = created.json()
    assert len(item["photos"]) == 1
    assert item["photos"][0]["original_filename"] == "IMG_0258.JPEG"
    assert item["photos"][0]["mime_type"] == "image/mpo"
    preview = client.get(item["photos"][0]["image_url"])
    assert preview.status_code == 200
    assert_orientation_3_jpeg(preview.content)
    assert any(
        "MPO auxiliary frame decode failed frame=1" in message
        and "using valid primary frame 0" in message
        for message in diagnostics
    )

    with factory() as session:
        photos = session.scalars(select(Photo)).all()
        assert len(photos) == 1
        photo = photos[0]
        assert photo.mime_type == "image/mpo"
        assert photo.checksum_sha256 == source_checksum
        assert Path(photo.file_path).read_bytes() == source
        processing = resolve_photo_for_processing(photo)
        assert processing.mime_type == "image/jpeg"
        assert processing.file_path.read_bytes() == preview.content
        assert processing.checksum_sha256 == hashlib.sha256(
            preview.content
        ).hexdigest()
        assert (processing.width, processing.height) == (40, 24)
    assert len(list(originals.rglob("*.mpo"))) == 1
    assert len(list((originals.parent / "normalized").rglob("*.jpg"))) == 1


def test_invalid_primary_mpo_is_rejected_without_persisted_state(store):
    client, factory, originals = store
    source = mpo_with_invalid_primary()
    with Image.open(BytesIO(source)) as image:
        assert image.format == "MPO" and image.n_frames == 2
        with pytest.raises(OSError, match="broken data stream"):
            image.load()

    response = upload(client, source, filename="BAD_PRIMARY.JPEG")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "uploaded file is not a decodable image"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 0
        assert session.scalar(select(func.count()).select_from(ProductIntakeItem)) == 0
    assert not originals.exists()
    assert not (originals.parent / "normalized").exists()
    assert_no_canonical(factory)


def test_stale_primary_size_and_out_of_bounds_auxiliary_are_recovered(
    store,
    monkeypatch,
):
    client, factory, originals = store
    diagnostics = []

    def capture_diagnostic(message, *args):
        diagnostics.append(message % args)

    monkeypatch.setattr(
        "app.services.image_processing.logger.warning",
        capture_diagnostic,
    )
    source = mpo_with_stale_mpf_metadata()
    source_checksum = hashlib.sha256(source).hexdigest()
    with Image.open(BytesIO(source)) as image:
        assert image.format == "MPO" and image.n_frames == 2
        entries = image.mpinfo[0xB002]
        offsets = getattr(image, "_MpoImageFile__mpoffsets")
        assert entries[0]["Size"] > len(source)
        assert offsets[1] + entries[1]["Size"] > len(source)
        assert image.tell() == 0 and image.getexif().get(274) == 3
        image.load()
        with pytest.raises(SyntaxError, match="not a JPEG file"):
            image.seek(1)

    created = upload(client, source, filename="IMG_0257.JPEG")

    assert created.status_code == 201, created.text
    item = created.json()
    assert len(item["photos"]) == 1
    assert item["photos"][0]["original_filename"] == "IMG_0257.JPEG"
    assert item["photos"][0]["mime_type"] == "image/mpo"
    preview = client.get(item["photos"][0]["image_url"])
    assert preview.status_code == 200
    assert_orientation_3_jpeg(preview.content)
    assert any(
        "malformed MPO/MPF metadata ignored frame=0" in message
        for message in diagnostics
    )
    assert any(
        "malformed MPO/MPF metadata ignored frame=1" in message
        for message in diagnostics
    )

    with factory() as session:
        photo = session.scalar(select(Photo))
        assert photo.mime_type == "image/mpo"
        assert photo.checksum_sha256 == source_checksum
        assert Path(photo.file_path).read_bytes() == source
        processing = resolve_photo_for_processing(photo)
        assert processing.mime_type == "image/jpeg"
        assert processing.file_path.read_bytes() == preview.content
    assert len(list(originals.rglob("*.mpo"))) == 1
    assert len(list((originals.parent / "normalized").rglob("*.jpg"))) == 1


@pytest.mark.parametrize(
    "legacy_version",
    [LEGACY_PROCESSING_VERSION, LEGACY_MPO_PROCESSING_VERSION],
)
def test_v3_resolution_reuses_valid_legacy_mpo_processing_asset(
    store,
    monkeypatch,
    legacy_version,
):
    client, factory, originals = store
    source = mpo(orientation=6)
    item = upload(client, source).json()
    source_checksum = hashlib.sha256(source).hexdigest()
    normalized = originals.parent / "normalized"
    current_index = (
        normalized
        / "index"
        / PROCESSING_VERSION
        / source_checksum[:2]
        / f"{source_checksum}.json"
    )
    record = json.loads(current_index.read_text(encoding="utf-8"))
    expected_asset_checksum = record["checksum_sha256"]
    legacy_index = (
        normalized
        / "index"
        / legacy_version
        / source_checksum[:2]
        / f"{source_checksum}.json"
    )
    legacy_index.parent.mkdir(parents=True)
    record["version"] = legacy_version
    if legacy_version == LEGACY_MPO_PROCESSING_VERSION:
        record.pop("mime_type")
    legacy_index.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    current_index.unlink()

    def no_reencode(*args):
        raise AssertionError("valid legacy MPO asset must be reused")

    monkeypatch.setattr(
        "app.services.image_processing.normalize_image_for_processing",
        no_reencode,
    )
    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        asset = resolve_photo_for_processing(photo)
    assert asset.checksum_sha256 == expected_asset_checksum
    assert current_index.exists()
    assert json.loads(current_index.read_text(encoding="utf-8"))["mime_type"] == "image/jpeg"
    assert len(list(normalized.rglob("*.jpg"))) == 1


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


@pytest.mark.parametrize(
    ("image_format", "mime_type", "extension"),
    [("PNG", "image/png", ".png"), ("WEBP", "image/webp", ".webp")],
)
def test_png_and_webp_orientation_metadata_is_physically_applied(
    store,
    image_format,
    mime_type,
    extension,
):
    client, factory, originals = store
    source = asymmetric_oriented_image(image_format)
    item = upload(client, source).json()
    preview = client.get(item["photos"][0]["image_url"])
    assert preview.headers["content-type"] == mime_type
    assert preview.content != source
    assert_upright_image(preview.content, image_format)
    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        assert Path(photo.file_path).read_bytes() == source
        assert photo.checksum_sha256 == hashlib.sha256(source).hexdigest()
        assert (photo.width, photo.height) == (40, 24)
        asset = resolve_photo_for_processing(photo)
        assert asset.file_path.suffix == extension
        assert asset.file_path.read_bytes() == preview.content
        assert asset.mime_type == mime_type
        assert (asset.width, asset.height) == (24, 40)
    assert len(list((originals.parent / "normalized").rglob(f"*{extension}"))) == 1


def test_windows_png_ignores_stale_xmp_orientation_and_bypasses_v2_cache(store):
    client, factory, originals = store
    source = xmp_only_oriented_png()
    source_checksum = hashlib.sha256(source).hexdigest()
    with Image.open(BytesIO(source)) as image:
        assert image.format == "PNG"
        assert image.size == (24, 40)
        assert "exif" not in image.info
        assert image.getexif().get(274) == 6
        image.load()
        assert image.getpixel((12, 8))[:3] == (255, 0, 0)
        assert image.getpixel((12, 32))[:3] == (0, 0, 255)

    created = upload(
        client,
        source,
        filename="IMG_0278.png",
        mime_type="image/png",
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["photos"][0]["image_url"].endswith(
        f"?v={PROCESSING_VERSION}"
    )
    photo_id = uuid.UUID(item["photos"][0]["id"])
    normalized = originals.parent / "normalized"
    current_index = (
        normalized
        / "index"
        / PROCESSING_VERSION
        / source_checksum[:2]
        / f"{source_checksum}.json"
    )
    current_record = json.loads(current_index.read_text(encoding="utf-8"))
    expected_checksum = current_record["checksum_sha256"]
    current_index.unlink()

    rotated = BytesIO()
    with Image.open(BytesIO(source)) as image:
        sideways = ImageOps.exif_transpose(image)
        with sideways:
            sideways.info.clear()
            sideways.save(
                rotated,
                format="PNG",
                optimize=False,
                compress_level=6,
            )
    stale_bytes = rotated.getvalue()
    stale_checksum = hashlib.sha256(stale_bytes).hexdigest()
    stale_path = normalized / stale_checksum[:2] / f"{stale_checksum}.png"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(stale_bytes)
    legacy_index = (
        normalized
        / "index"
        / LEGACY_PROCESSING_VERSION
        / source_checksum[:2]
        / f"{source_checksum}.json"
    )
    legacy_index.parent.mkdir(parents=True, exist_ok=True)
    legacy_index.write_text(
        json.dumps(
            {
                "version": LEGACY_PROCESSING_VERSION,
                "source_checksum_sha256": source_checksum,
                "checksum_sha256": stale_checksum,
                "mime_type": "image/png",
                "file_size_bytes": len(stale_bytes),
                "width": 40,
                "height": 24,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    preview = client.get(item["photos"][0]["image_url"])
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-cache"
    assert preview.headers["content-type"] == "image/png"
    assert hashlib.sha256(preview.content).hexdigest() == expected_checksum
    assert hashlib.sha256(preview.content).hexdigest() != stale_checksum
    with Image.open(BytesIO(preview.content)) as image:
        assert image.size == (24, 40)
        assert image.getexif().get(274, 1) == 1
        assert "XML:com.adobe.xmp" not in image.info
        image.load()
        assert image.getpixel((12, 8))[:3] == (255, 0, 0)
        assert image.getpixel((12, 32))[:3] == (0, 0, 255)

    class Vision:
        name = "openai"

        def extract(self, request):
            assert request.images[0].content == preview.content
            return VisionExtractionResponse(
                ProductExtractionResult.model_validate({
                    "brand_name": observation("Phone Brand"),
                    "product_name": observation("Phone Product"),
                    "flavor": observation("Plain"),
                    "size_value": observation("1"),
                    "size_unit": observation("kg"),
                    "servings": observation(None),
                }),
                {},
            )

    response = client.post(
        f"/api/product-intake/items/{item['id']}/extractions",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 202
    JobWorker(
        factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(factory, Vision())},
    ).run_once()

    with factory() as session:
        photo = session.get(Photo, photo_id)
        assert Path(photo.file_path).read_bytes() == source
        assert photo.checksum_sha256 == source_checksum
        asset = resolve_photo_for_processing(photo)
        assert asset.file_path.read_bytes() == preview.content
        enqueue_image_enhancement(session, source_photo_id=photo_id)
        session.commit()

    class Enhancement:
        name = "openai"

        def enhance(self, request):
            assert request.source.content == preview.content
            return ImageEnhancementResult(preview.content, "png", {})

    JobWorker(
        factory,
        {
            IMAGE_ENHANCEMENT_JOB_TYPE: ImageEnhancementJobHandler(
                factory,
                Enhancement(),
                processed_dir=originals.parent / "processed",
            )
        },
    ).run_once()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 1
        derived = session.scalar(select(DerivedImage))
        assert Path(derived.file_path).read_bytes() == preview.content
    assert legacy_index.exists()
    assert current_index.exists()


@pytest.mark.parametrize("orientation", [None, 1])
def test_canonical_jpeg_with_absent_or_identity_orientation_keeps_exact_bytes(
    store,
    orientation,
):
    client, factory, originals = store
    source = asymmetric_jpeg(orientation=orientation)
    item = upload(client, source).json()
    assert client.get(item["photos"][0]["image_url"]).content == source
    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        asset = resolve_photo_for_processing(photo)
        assert asset.file_path == Path(photo.file_path)
        assert asset.checksum_sha256 == hashlib.sha256(source).hexdigest()
    assert not (originals.parent / "normalized").exists()


@pytest.mark.parametrize("kind", ["malformed_index", "empty_index"])
def test_decodable_jpeg_with_stale_mpf_metadata_is_safely_normalized(store, kind):
    client, factory, originals = store
    source = malformed_mpf_jpeg(kind)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with Image.open(BytesIO(source)) as image:
            assert image.format == "JPEG"
            has_mpf_metadata = "mp" in image.info
            image.verify()
    assert has_mpf_metadata or any(
        "malformed MPO" in str(item.message) for item in caught
    )

    response = upload(client, source)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["photos"][0]["mime_type"] == "image/jpeg"
    preview = client.get(item["photos"][0]["image_url"])
    assert preview.status_code == 200
    assert_upright_jpeg(preview.content)

    with factory() as session:
        photo = session.get(Photo, uuid.UUID(item["photos"][0]["id"]))
        assert photo.mime_type == "image/jpeg"
        assert photo.checksum_sha256 == hashlib.sha256(source).hexdigest()
        assert Path(photo.file_path).read_bytes() == source
        assert (photo.width, photo.height) == (40, 24)
        asset = resolve_photo_for_processing(photo)
        assert asset.file_path.read_bytes() == preview.content
        assert asset.checksum_sha256 == hashlib.sha256(preview.content).hexdigest()
        assert (asset.width, asset.height) == (24, 40)
    assert len(list(originals.rglob("*.jpg"))) == 1
    assert len(list((originals.parent / "normalized").rglob("*.jpg"))) == 1


@pytest.mark.parametrize("failure", ["corrupt_jpeg", "unsupported"])
def test_bad_sources_fail_without_rows_or_normalized_orphans(store, failure):
    client, factory, originals = store
    source = mpo()
    if failure == "corrupt_jpeg":
        source = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
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


def test_truncated_auxiliary_region_is_ignored_when_primary_fully_decodes(store):
    client, factory, _ = store
    source = mpo()[:-40]
    with Image.open(BytesIO(source)) as image:
        assert image.format == "MPO" and image.n_frames == 2
        image.load()

    response = upload(client, source, filename="TRUNCATED_AUX.JPEG")

    assert response.status_code == 201, response.text
    with factory() as session:
        photo = session.scalar(select(Photo))
        assert photo.mime_type == "image/mpo"
        assert photo.checksum_sha256 == hashlib.sha256(source).hexdigest()
        assert Path(photo.file_path).read_bytes() == source


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


def test_exif_oriented_jpeg_uses_one_canonical_asset_end_to_end(store):
    client, factory, originals = store
    source = asymmetric_jpeg(orientation=6)
    source_checksum = hashlib.sha256(source).hexdigest()
    created = upload(client, source)
    assert created.status_code == 201, created.text
    item = created.json()
    photo_id = uuid.UUID(item["photos"][0]["id"])
    assert item["photos"][0]["mime_type"] == "image/jpeg"

    with factory() as session:
        photo = session.get(Photo, photo_id)
        assert photo.checksum_sha256 == source_checksum
        assert Path(photo.file_path).read_bytes() == source
        assert (photo.width, photo.height) == (40, 24)
        first = resolve_photo_for_processing(photo)
        second = resolve_photo_for_processing(photo)
        assert first == second
        processing_bytes = first.file_path.read_bytes()
        assert first.checksum_sha256 == hashlib.sha256(processing_bytes).hexdigest()
        assert first.mime_type == "image/jpeg"
        assert (first.width, first.height) == (24, 40)
    assert_upright_jpeg(processing_bytes)

    preview = client.get(item["photos"][0]["image_url"])
    assert preview.status_code == 200
    assert preview.content == processing_bytes
    assert_upright_jpeg(preview.content)

    class Vision:
        name = "openai"

        def extract(self, request):
            assert len(request.images) == 1
            assert request.images[0].mime_type == "image/jpeg"
            assert request.images[0].content == processing_bytes
            assert_upright_jpeg(request.images[0].content)
            return VisionExtractionResponse(
                ProductExtractionResult.model_validate({
                    "brand_name": observation("Phone Brand"),
                    "product_name": observation("Phone Product"),
                    "flavor": observation("Plain"),
                    "size_value": observation("1"),
                    "size_unit": observation("kg"),
                    "servings": observation(None),
                }),
                {},
            )

    response = client.post(
        f"/api/product-intake/items/{item['id']}/extractions",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 202
    JobWorker(
        factory,
        {PRODUCT_EXTRACTION_JOB_TYPE: ProductExtractionJobHandler(factory, Vision())},
    ).run_once()
    assert client.get(f"/api/product-intake/items/{item['id']}").json()["status"] == "review_required"

    from app.services.categories import create_category

    with factory() as session:
        category = create_category(session, name="Test Category")
        session.commit()
        category_id = category.id
    response = client.post(
        f"/api/product-intake/items/{item['id']}/promotion",
        json=promotion_body(
            category=str(category_id),
            prices=[{"intake_sku_index": 0, "amount": "100", "currency": "PYG"}],
        ),
    )
    assert response.status_code == 201, response.text
    product_id = uuid.UUID(response.json()["product_id"])

    with factory() as session:
        photo = session.get(Photo, photo_id)
        assert photo.product_id == product_id
        assert photo.mime_type == "image/jpeg"
        assert Path(photo.file_path).read_bytes() == source
        summary = get_product_image_editorial_summary(session, product_id)
        snapshot = create_catalog_snapshot(
            session,
            CatalogSnapshotCreate(product_ids=[product_id], currency="PYG"),
            storage_root=originals.parent,
        )
        data = read_catalog_snapshot_data(session, snapshot.id)
        hero = data.sections[0].products[0].hero
        assert hero.source_original_asset.checksum_sha256 == source_checksum
        assert hero.source_original_asset.storage_relative_path.startswith("originals/")
        assert (hero.source_original_asset.width, hero.source_original_asset.height) == (40, 24)
        assert hero.presentation_asset.storage_relative_path.startswith("normalized/")
        assert hero.presentation_asset.checksum_sha256 == hashlib.sha256(processing_bytes).hexdigest()
        assert (hero.presentation_asset.width, hero.presentation_asset.height) == (24, 40)
        view = build_catalog_render_view_model(
            data,
            normalize_catalog_render_config(),
            storage_root=originals.parent,
        )
        catalog_bytes = base64.b64decode(
            view.sections[0].products[0].image_data_uri.split(",", 1)[1]
        )
        assert catalog_bytes == processing_bytes
        job = enqueue_image_enhancement(session, source_photo_id=photo_id)
        assert job.payload["source_checksum_sha256"] == source_checksum
        session.commit()

    assert client.get(summary.original_preview_url).content == processing_bytes
    builder_preview = client.get(f"/api/catalog-builder/products/{product_id}/image")
    assert builder_preview.content == processing_bytes
    assert_upright_jpeg(builder_preview.content)

    returned = asymmetric_jpeg(
        orientation=6,
        left=(0, 255, 0),
        right=(255, 255, 0),
    )

    class Enhancement:
        name = "openai"

        def enhance(self, request):
            assert request.source.mime_type == "image/jpeg"
            assert request.source.content == processing_bytes
            assert request.source.checksum_sha256 == hashlib.sha256(processing_bytes).hexdigest()
            assert_upright_jpeg(request.source.content)
            return ImageEnhancementResult(returned, "jpeg", {})

    JobWorker(
        factory,
        {
            IMAGE_ENHANCEMENT_JOB_TYPE: ImageEnhancementJobHandler(
                factory,
                Enhancement(),
                processed_dir=originals.parent / "processed",
            )
        },
    ).run_once()

    with factory() as session:
        derived = session.scalar(select(DerivedImage))
        assert derived.source_photo_id == photo_id
        assert derived.mime_type == "image/jpeg"
        assert (derived.width, derived.height) == (24, 40)
        derived_bytes = Path(derived.file_path).read_bytes()
        assert derived.checksum_sha256 == hashlib.sha256(derived_bytes).hexdigest()
        assert session.scalar(select(func.count()).select_from(Photo)) == 1
        assert Path(session.get(Photo, photo_id).file_path).read_bytes() == source
    assert derived_bytes != returned
    assert_upright_jpeg(
        derived_bytes,
        top=(0, 255, 0),
        bottom=(255, 255, 0),
    )
    assert len(list((originals.parent / "normalized").rglob("*.jpg"))) == 1


def test_mpo_extraction_promotion_enhancement_and_frozen_catalog(store):
    client, factory, originals = store
    source = mpo_with_stale_mpf_metadata()
    created = upload(client, source)
    assert created.status_code == 201, created.text
    item = created.json()
    photo_id = uuid.UUID(item["photos"][0]["id"])
    preview = client.get(item["photos"][0]["image_url"])
    assert preview.status_code == 200
    assert_orientation_3_jpeg(preview.content)

    class Vision:
        name = "openai"
        def extract(self, request):
            assert len(request.images) == 1
            assert request.images[0].mime_type == "image/jpeg"
            assert request.images[0].content == preview.content
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
        assert_orientation_3_jpeg(base64.b64decode(uri.split(",", 1)[1]))
        html = render_catalog_html(view, resolve_catalog_template(config.template_key))
        assert len(PdfReader(BytesIO(ChromiumCatalogPdfRenderer().render(html, config).pdf_bytes)).pages) >= 1
        assert snapshot.payload == old_payload and snapshot.content_hash == old_hash
        job = enqueue_image_enhancement(session, source_photo_id=photo_id)
        assert job.payload["source_checksum_sha256"] == photos[0].checksum_sha256
        session.commit()

    assert client.get(summary.original_preview_url).content == preview.content
    assert (
        client.get(f"/api/catalog-builder/products/{product_id}/image").content
        == preview.content
    )

    class Enhancement:
        name = "openai"
        def enhance(self, request):
            assert request.source.mime_type == "image/jpeg"
            assert request.source.content == preview.content
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
