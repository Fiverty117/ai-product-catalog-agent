"""Offline, synthetic v4 cover page and product-page regression checks."""

import base64
import hashlib
import re
import shutil
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from app.domain.schemas import CatalogRenderConfig, ResolvedCatalogBranding, ResolvedCatalogCover
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.rendering.catalog_themes import resolve_catalog_theme
from app.scripts.catalog_visual_stress import build_stress_view_model
from app.services.catalog_rendering import hash_catalog_template, render_catalog_html, resolve_catalog_template
from app.services.catalog_cover_assets import load_frozen_catalog_cover_hero

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


def _view(layout: str, theme: str, cover_key: str | None, *, hero: bool = False, storage_root: Path | None = None):
    config = CatalogRenderConfig(layout=layout, template_key="grabelan-catalog-v3")
    branding = ResolvedCatalogBranding(
        schema_version="catalog-branding-v1", source_profile_id=uuid.uuid4(),
        profile_key="qa", display_name="CATÁLOGO QA - DATOS SINTÉTICOS",
        primary_color="#596B3F", accent_color="#B08A4A",
    )
    image_uri = None
    frozen_hero = None
    if hero:
        assert storage_root is not None
        image = BytesIO()
        Image.new("RGB", (1300, 65), (170, 190, 160)).save(image, format="PNG")
        data = image.getvalue()
        checksum = hashlib.sha256(data).hexdigest()
        relative = f"covers/{checksum[:2]}/{checksum}.png"
        path = storage_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        from app.domain.schemas import FrozenCatalogCoverHero
        frozen_hero = FrozenCatalogCoverHero(
            source_cover_asset_id=uuid.uuid4(), checksum_sha256=checksum,
            mime_type="image/png", file_size_bytes=len(data), width=1300, height=65,
            storage_relative_path=relative,
        )
        image_uri = "data:image/png;base64," + base64.b64encode(load_frozen_catalog_cover_hero(
            frozen_hero, storage_root=storage_root)).decode("ascii")
    cover = ResolvedCatalogCover(
        schema_version="catalog-cover-v1", enabled=cover_key is not None,
        cover_key=cover_key, cover_version="1" if cover_key else None,
        title="Edición QA única" if cover_key else None,
        subtitle="Subtítulo de prueba" if cover_key else None,
        edition_label="2026" if cover_key else None,
        show_publisher_logo=False,
        # The PDF fixture supplies the verified hero bytes directly; asset lineage is tested separately.
        hero=frozen_hero,
    ) if cover_key != "hero" else None
    if cover_key == "hero":
        cover = ResolvedCatalogCover(
            schema_version="catalog-cover-v1", enabled=True, cover_key="hero", cover_version="1",
            title="Edición QA única", subtitle="Subtítulo de prueba", edition_label="2026",
            hero=frozen_hero,
        )
    view = build_stress_view_model(config).model_copy(update={
        "theme": resolve_catalog_theme(theme, "1", branding),
        "cover": cover,
        "cover_hero_data_uri": image_uri,
    })
    return config, view


def _pdf(config, view) -> PdfReader:
    template = resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT)
    html = render_catalog_html(view, template)
    assert "http://" not in html and "https://" not in html and "file://" not in html
    return PdfReader(BytesIO(ChromiumCatalogPdfRenderer().render(html, config).pdf_bytes))


@pytest.mark.parametrize("cover_key,theme,layout,hero", [
    (None, "premium", "classic", False),
    ("minimal", "minimal", "classic", False),
    ("minimal", "premium", "dense", False),
    ("editorial", "premium", "classic", False),
    ("editorial", "organic", "compact", False),
    ("editorial", "bold", "dense", False),
    ("hero", "premium", "classic", True),
    ("hero", "organic", "compact", True),
])
def test_v4_cover_combinations_preserve_product_content(cover_key, theme, layout, hero, tmp_path):
    config, view = _view(layout, theme, cover_key, hero=hero, storage_root=tmp_path)
    reader = _pdf(config, view)
    assert 2 <= len(reader.pages) <= 15
    first = re.sub(r"\s+", " ", reader.pages[0].extract_text() or "")
    all_text = re.sub(r"\s+", " ", " ".join(page.extract_text() or "" for page in reader.pages))
    if cover_key:
        assert "Edición QA única" in first
        assert "Subtítulo de prueba" in first
        assert "2026" in first
        assert "Kombucha sintética" not in first
        assert "Kombucha sintética" in " ".join(page.extract_text() or "" for page in reader.pages[1:])
    else:
        assert "Edición QA única" not in first
    assert "Gs. 999.999.999" in all_text
    assert "densidad de tarjetas" in all_text.casefold()
    if layout == "compact":
        assert "Descripción sintética de densidad" not in all_text
    else:
        assert "Descripción sintética de densidad" in all_text


def test_cover_adds_exactly_one_page_and_disabled_v4_preserves_v3_text():
    config, without = _view("classic", "premium", None)
    plain = _pdf(config, without)
    _, with_cover = _view("classic", "premium", "editorial")
    covered = _pdf(config, with_cover)
    assert len(covered.pages) == len(plain.pages) + 1
    for covered_page, plain_page in zip(covered.pages[1:], plain.pages):
        assert covered_page.extract_text() == plain_page.extract_text()
    v3_config = CatalogRenderConfig(layout="classic", template_key="grabelan-catalog-v2")
    v3_view = without.model_copy(update={"cover": None, "cover_hero_data_uri": None})
    v3 = _pdf(v3_config, v3_view)
    assert len(v3.pages) == len(plain.pages)
    for v4_page, v3_page in zip(plain.pages, v3.pages):
        assert re.sub(r"\s+", " ", v4_page.extract_text() or "").strip() == re.sub(
            r"\s+", " ", v3_page.extract_text() or ""
        ).strip()


def test_editorial_without_hero_uses_only_meaningful_media():
    config, text_only = _view("classic", "premium", "editorial")
    template = resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT)
    text_html = render_catalog_html(text_only, template).split("</section>", 1)[0]
    assert "cover-editorial cover-text-only" in text_html
    assert 'class="cover-logo-media"' not in text_html
    assert 'class="cover-hero"' not in text_html

    image = BytesIO()
    Image.new("RGBA", (600, 250), (89, 107, 63, 255)).save(image, format="PNG")
    logo_uri = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
    logo_branding = text_only.branding.model_copy(update={"logo_data_uri": logo_uri})
    logo_cover = text_only.cover.model_copy(update={"show_publisher_logo": True})
    logo_only = text_only.model_copy(update={"branding": logo_branding, "cover": logo_cover})
    logo_html = render_catalog_html(logo_only, template).split("</section>", 1)[0]
    assert "cover-editorial cover-has-logo" in logo_html
    assert logo_html.count('class="cover-logo-media"') == 1
    assert 'class="cover-logo"' not in logo_html
    assert 'class="cover-hero"' not in logo_html

    without_cover = logo_only.model_copy(update={
        "cover": ResolvedCatalogCover(schema_version="catalog-cover-v1", enabled=False),
    })
    covered_pdf = _pdf(config, logo_only)
    plain_pdf = _pdf(config, without_cover)
    assert len(covered_pdf.pages) == len(plain_pdf.pages) + 1
    assert "Edición QA única" in " ".join((covered_pdf.pages[0].extract_text() or "").split())
    assert "Kombucha sintética" not in (covered_pdf.pages[0].extract_text() or "")
    assert "Producto de prueba con proteína" in " ".join(
        (covered_pdf.pages[1].extract_text() or "").split()
    )


def test_long_editorial_cover_remains_one_page():
    config, view = _view("classic", "premium", "editorial")
    title = "Catálogo mayorista de bienestar y productos naturales para tiendas locales 2026."
    assert len(title) == 80
    subtitle = (
        "Selección de productos con descripciones extensas para comprobar la lectura "
        "en un diseño editorial de portada, incluso cuando el texto ocupa varias líneas."
    )
    cover = view.cover.model_copy(update={
        "title": title,
        "subtitle": subtitle,
        "edition_label": "Edición mayorista para septiembre de dos mil veintiséis",
    })
    long_view = view.model_copy(update={"cover": cover})
    template = resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT)
    assert "cover-title-long" in render_catalog_html(long_view, template).split("</section>", 1)[0]
    reader = _pdf(config, long_view)
    first = " ".join((reader.pages[0].extract_text() or "").split())
    assert title in first
    assert subtitle in first
    assert "Kombucha sintética" not in first
    assert "Kombucha sintética" in " ".join(page.extract_text() or "" for page in reader.pages[1:])


def test_v4_polish_css_is_hashed_without_changing_v3_hash(tmp_path):
    copied = tmp_path / "templates"
    shutil.copytree(TEMPLATE_ROOT, copied)
    v3 = resolve_catalog_template("grabelan-catalog-v2", template_root=copied)
    v4 = resolve_catalog_template("grabelan-catalog-v3", template_root=copied)
    v3_hash = hash_catalog_template(v3)
    v4_hash = hash_catalog_template(v4)
    v4.cover_css_path.write_text(
        v4.cover_css_path.read_text(encoding="utf-8") + "\n/* polish hash check */\n",
        encoding="utf-8",
    )
    assert hash_catalog_template(v4) != v4_hash
    assert hash_catalog_template(v3) == v3_hash
