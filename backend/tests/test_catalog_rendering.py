import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from markupsafe import escape
from pydantic import ValidationError
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, CatalogSnapshot
from app.db.session import create_sqlite_engine
from app.domain.enums import CatalogHeroPhotoSource, PhotoPresentationAssetType
from app.domain.schemas import (
    CatalogHeroSnapshot,
    CatalogProductSnapshot,
    CatalogRenderConfig,
    CatalogRenderJobPayload,
    CatalogSectionSnapshot,
    CatalogSnapshotData,
    CatalogVariantSnapshot,
    FrozenCatalogAsset,
    FrozenCatalogCategory,
    FrozenCatalogPrice,
)
from app.services.catalog_rendering import (
    CATALOG_RENDERER_VERSION,
    CatalogRenderError,
    InvalidCatalogPdfError,
    InvalidCatalogRenderConfigError,
    InvalidCatalogTemplateError,
    CatalogPdfStorageError,
    SnapshotAssetIntegrityError,
    build_catalog_render_idempotency_key,
    build_catalog_render_view_model,
    enqueue_catalog_render,
    hash_catalog_template,
    normalize_catalog_render_config,
    render_catalog_html,
    resolve_catalog_template,
    store_catalog_pdf,
)
from app.services.catalog_snapshots import hash_catalog_snapshot_data

AS_OF = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'rendering.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.info["storage_root"] = tmp_path / "storage"
        yield session
    engine.dispose()


def image_bytes(color=(10, 20, 30)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (13, 11), color).save(output, format="PNG")
    return output.getvalue()


def frozen_asset(storage_root: Path, *, color=(10, 20, 30)) -> FrozenCatalogAsset:
    content = image_bytes(color)
    checksum = hashlib.sha256(content).hexdigest()
    relative = f"originals/{checksum[:2]}/{checksum}.png"
    path = storage_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return FrozenCatalogAsset(
        checksum_sha256=checksum,
        mime_type="image/png",
        file_size_bytes=len(content),
        width=13,
        height=11,
        storage_relative_path=relative,
    )


def variant(
    *,
    flavor=None,
    size_value=None,
    size_unit=None,
    amount="180000.0000",
) -> CatalogVariantSnapshot:
    return CatalogVariantSnapshot(
        source_sku_id=uuid.uuid4(),
        external_sku=None,
        flavor=flavor,
        size_value=Decimal(size_value) if size_value is not None else None,
        size_unit=size_unit,
        servings=None,
        price=FrozenCatalogPrice(
            source_price_id=uuid.uuid4(),
            amount=Decimal(amount),
            currency="PYG",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source="manual",
        ),
    )


def snapshot_data(
    storage_root: Path,
    *,
    product_name="Premium Whey",
    brand_name="Landerfit",
    category_name="Suplementos",
    short_description=None,
    variants=None,
    asset=None,
) -> CatalogSnapshotData:
    asset = asset or frozen_asset(storage_root)
    hero = CatalogHeroSnapshot(
        source_photo_id=uuid.uuid4(),
        source_photo_owner_type=CatalogHeroPhotoSource.PRODUCT,
        source_photo_owner_sku_id=None,
        source_original_asset=asset,
        presentation_type=PhotoPresentationAssetType.ORIGINAL,
        source_derived_image_id=None,
        presentation_asset=asset,
    )
    product = CatalogProductSnapshot(
        source_brand_id=uuid.uuid4(),
        brand_name=brand_name,
        brand_identity_key=brand_name.casefold(),
        source_product_id=uuid.uuid4(),
        product_name=product_name,
        product_identity_key=product_name.casefold(),
        short_description=short_description,
        hero=hero,
        variants=variants or [variant()],
    )
    return CatalogSnapshotData(
        schema_version="catalog-snapshot-v1",
        currency="PYG",
        as_of=AS_OF,
        sections=[
            CatalogSectionSnapshot(
                category=FrozenCatalogCategory(
                    source_category_id=uuid.uuid4(),
                    name=category_name,
                    identity_key=category_name.casefold(),
                    sort_order=10,
                ),
                products=[product],
            )
        ],
    )


def persist_snapshot(session: Session, data: CatalogSnapshotData) -> CatalogSnapshot:
    snapshot = CatalogSnapshot(
        schema_version=data.schema_version,
        currency=data.currency,
        as_of=data.as_of,
        payload=data.model_dump(mode="json"),
        content_hash=hash_catalog_snapshot_data(data),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def valid_pdf(page_count=1) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=595, height=842)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_render_config_is_strict_normalized_and_locale_aware() -> None:
    assert normalize_catalog_render_config().locale == "es-PY"
    assert normalize_catalog_render_config({"locale": "en-US"}).locale == "en-US"
    with pytest.raises(InvalidCatalogRenderConfigError):
        normalize_catalog_render_config({"locale": "not-a-real-locale"})
    with pytest.raises(InvalidCatalogRenderConfigError):
        normalize_catalog_render_config({"unknown": True})
    with pytest.raises(ValidationError):
        CatalogRenderConfig(page_size="Letter")


def test_view_model_preserves_sections_groups_variants_and_formats_decimal(
    session: Session,
) -> None:
    data = snapshot_data(
        session.info["storage_root"],
        short_description="Descripcion congelada para el catalogo.",
        variants=[
            variant(flavor="Vanilla"),
            variant(size_value="250.500000", size_unit="g"),
            variant(flavor="Chocolate", size_value="2", size_unit="lb"),
            variant(),
        ],
    )
    view = build_catalog_render_view_model(
        data,
        normalize_catalog_render_config(),
        storage_root=session.info["storage_root"],
    )

    assert [section.category_name for section in view.sections] == ["Suplementos"]
    assert len(view.sections[0].products) == 1
    product = view.sections[0].products[0]
    assert [item.label for item in product.variants] == [
        "Vanilla",
        "250,5 g",
        "Chocolate / 2 lb",
        None,
    ]
    assert all(item.price_display for item in product.variants)
    assert "180.000" in product.variants[0].price_display
    assert view.currency == "PYG"
    assert product.short_description == "Descripcion congelada para el catalogo."
    assert product.image_data_uri.startswith("data:image/png;base64,")


def test_html_is_offline_escaped_and_contains_only_snapshot_content(
    session: Session,
) -> None:
    product_name = '<script>alert("x")</script>'
    category_name = "Nutrition & Wellness"
    data = snapshot_data(
        session.info["storage_root"],
        product_name=product_name,
        brand_name="Brand <unsafe>",
        category_name=category_name,
        short_description="Texto congelado & seguro.",
        variants=[variant(flavor="Vanilla"), variant(flavor="Chocolate")],
    )
    config = normalize_catalog_render_config()
    view = build_catalog_render_view_model(
        data, config, storage_root=session.info["storage_root"]
    )
    rendered = render_catalog_html(
        view,
        resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT),
    )

    assert product_name not in rendered
    assert str(escape(product_name)) in rendered
    assert "Brand &lt;unsafe&gt;" in rendered
    assert "Nutrition &amp; Wellness" in rendered
    assert "Texto congelado &amp; seguro." in rendered
    assert rendered.count('class="product-card"') == 1
    assert "Vanilla" in rendered and "Chocolate" in rendered
    assert "beneficio" not in rendered.casefold()
    assert "http://" not in rendered and "https://" not in rendered
    assert "file://" not in rendered
    assert str(session.info["storage_root"]) not in rendered
    assert "data:image/png;base64," in rendered
    assert 'class="logo"' not in rendered.casefold()


def test_classic_template_handles_long_content_and_multiple_variants(
    session: Session,
) -> None:
    product_name = (
        "Organic Fermented Plant Protein with Naturally Cultured Ingredients"
    )
    long_variant = (
        "Passion Fruit, Ginger and Botanical Blend / Family Presentation 2 kg"
    )
    data = snapshot_data(
        session.info["storage_root"],
        product_name=product_name,
        brand_name="A Long but Valid Independent Producer Name",
        category_name="Natural Foods, Ferments and Functional Pantry Staples",
        variants=[
            variant(flavor=long_variant),
            variant(flavor="Vanilla", amount="123456789.0000"),
            variant(),
        ],
    )
    config = normalize_catalog_render_config()
    view = build_catalog_render_view_model(
        data,
        config,
        storage_root=session.info["storage_root"],
    )
    rendered = render_catalog_html(
        view,
        resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT),
    )
    stylesheet = (TEMPLATE_ROOT / "catalog-v1.css").read_text(encoding="utf-8")

    assert rendered.count('class="product-card"') == 1
    assert rendered.count('class="variant-row') == 3
    assert product_name in rendered
    assert long_variant in rendered
    assert 'class="variant-label"' in rendered
    assert 'class="variant-price"' in rendered
    assert "--surface:" in stylesheet and "--accent:" in stylesheet
    assert "repeat(var(--products-per-row), minmax(0, 1fr))" in stylesheet
    assert "break-inside: avoid-page" in stylesheet
    assert "display: inline-grid" in stylesheet
    assert "page-break-inside: avoid" in stylesheet


def test_template_groups_ordered_products_into_print_safe_rows(
    session: Session,
) -> None:
    data = snapshot_data(session.info["storage_root"])
    config = normalize_catalog_render_config()
    view = build_catalog_render_view_model(
        data,
        config,
        storage_root=session.info["storage_root"],
    )
    first = view.sections[0].products[0]
    products = [
        first.model_copy(
            update={
                "source_product_id": uuid.uuid4(),
                "product_name": f"Ordered Product {number}",
            }
        )
        for number in range(1, 6)
    ]
    section = view.sections[0].model_copy(update={"products": products})
    view = view.model_copy(update={"sections": [section]})
    rendered = render_catalog_html(
        view,
        resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT),
    )
    stylesheet = (TEMPLATE_ROOT / "catalog-v1.css").read_text(encoding="utf-8")
    row_segments = rendered.split('<div class="product-row">')[1:]

    assert '<main style="--products-per-row: 2">' in rendered
    assert len(row_segments) == 3
    assert [row.count('class="product-card"') for row in row_segments] == [2, 2, 1]
    assert [rendered.index(f"Ordered Product {number}") for number in range(1, 6)] == sorted(
        rendered.index(f"Ordered Product {number}") for number in range(1, 6)
    )
    assert 'class="product-grid"' not in rendered
    assert "break-before: page" not in stylesheet
    assert "break-after: avoid-page" in stylesheet
    assert "page-break-after: avoid" in stylesheet
    assert ".product-row" in stylesheet
    assert "grid-template-columns: repeat(var(--products-per-row)" in stylesheet


def test_template_registry_and_content_hash_track_css(tmp_path) -> None:
    copied = tmp_path / "template"
    shutil.copytree(TEMPLATE_ROOT, copied)
    template = resolve_catalog_template(
        "grabelan-catalog-v1", template_root=copied
    )
    first = hash_catalog_template(template)
    assert first == hash_catalog_template(template)
    template.css_path.write_text(
        template.css_path.read_text(encoding="utf-8") + "\nbody { color: #111; }\n",
        encoding="utf-8",
    )
    assert hash_catalog_template(template) != first
    template.css_path.write_text(
        template.css_path.read_text(encoding="utf-8")
        + "\n.x { background: url(https://example.com/x.png); }\n",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCatalogTemplateError, match="external"):
        render_catalog_html(
            type("View", (), {"model_dump": lambda self, **kwargs: {}})(),
            template,
        )


@pytest.mark.parametrize("failure", ["missing", "checksum", "corrupt", "metadata"])
def test_frozen_presentation_asset_is_revalidated(
    session: Session, failure: str
) -> None:
    asset = frozen_asset(session.info["storage_root"])
    data = snapshot_data(session.info["storage_root"], asset=asset)
    path = session.info["storage_root"] / asset.storage_relative_path
    if failure == "missing":
        path.unlink()
    elif failure == "checksum":
        path.write_bytes(image_bytes((50, 60, 70)))
    elif failure == "corrupt":
        path.write_bytes(b"not an image")
        data.sections[0].products[0].hero.presentation_asset.checksum_sha256 = (
            hashlib.sha256(b"not an image").hexdigest()
        )
    else:
        data.sections[0].products[0].hero.presentation_asset.width = 999

    with pytest.raises(SnapshotAssetIntegrityError):
        build_catalog_render_view_model(
            data,
            normalize_catalog_render_config(),
            storage_root=session.info["storage_root"],
        )


def test_enqueue_is_idempotent_and_tracks_snapshot_template_config(
    session: Session, tmp_path
) -> None:
    snapshot = persist_snapshot(
        session, snapshot_data(session.info["storage_root"])
    )
    first = enqueue_catalog_render(
        session,
        catalog_snapshot_id=snapshot.id,
        template_root=TEMPLATE_ROOT,
    )
    same = enqueue_catalog_render(
        session,
        catalog_snapshot_id=snapshot.id,
        template_root=TEMPLATE_ROOT,
    )
    different_locale = enqueue_catalog_render(
        session,
        catalog_snapshot_id=snapshot.id,
        config={"locale": "en-US"},
        template_root=TEMPLATE_ROOT,
    )
    different_renderer = enqueue_catalog_render(
        session,
        catalog_snapshot_id=snapshot.id,
        template_root=TEMPLATE_ROOT,
        renderer_version="catalog-chromium-v2",
    )
    copied = tmp_path / "changed-template"
    shutil.copytree(TEMPLATE_ROOT, copied)
    css = copied / "catalog-v1.css"
    css.write_text(css.read_text(encoding="utf-8") + "\nbody { color: #111; }\n")
    different_template = enqueue_catalog_render(
        session,
        catalog_snapshot_id=snapshot.id,
        template_root=copied,
    )

    assert same.id == first.id
    assert len(
        {first.id, different_locale.id, different_renderer.id, different_template.id}
    ) == 4
    assert first.idempotency_key == build_catalog_render_idempotency_key(
        CatalogRenderJobPayload.model_validate(first.payload)
    )
    assert first.payload["renderer_version"] == CATALOG_RENDERER_VERSION


def test_corrupted_snapshot_is_rejected_before_enqueue(session: Session) -> None:
    snapshot = persist_snapshot(
        session, snapshot_data(session.info["storage_root"])
    )
    snapshot.content_hash = "0" * 64
    session.flush()

    with pytest.raises(CatalogRenderError):
        enqueue_catalog_render(
            session,
            catalog_snapshot_id=snapshot.id,
            template_root=TEMPLATE_ROOT,
        )
    assert session.scalars(select(CatalogSnapshot)).one().id == snapshot.id


def test_pdf_validation_content_addressing_and_safe_reuse(tmp_path) -> None:
    pdf = valid_pdf(page_count=2)
    catalogs = tmp_path / "storage" / "catalogs"
    first = store_catalog_pdf(pdf, catalogs_dir=catalogs)
    second = store_catalog_pdf(pdf, catalogs_dir=catalogs)

    assert first == second
    assert first.page_count == 2
    assert first.file_size_bytes == len(pdf)
    assert first.checksum_sha256 == hashlib.sha256(pdf).hexdigest()
    path = Path(first.file_path)
    assert path.is_relative_to(catalogs.resolve())
    assert path == catalogs.resolve() / first.checksum_sha256[:2] / (
        first.checksum_sha256 + ".pdf"
    )
    assert path.read_bytes() == pdf
    path.write_bytes(b"mismatched existing bytes")
    with pytest.raises(CatalogPdfStorageError, match="content identity"):
        store_catalog_pdf(pdf, catalogs_dir=catalogs)
    with pytest.raises(InvalidCatalogPdfError):
        store_catalog_pdf(b"not a PDF", catalogs_dir=catalogs)
