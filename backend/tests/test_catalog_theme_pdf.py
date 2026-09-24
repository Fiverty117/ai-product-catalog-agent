"""Offline synthetic PDF stress across the complete Theme × Layout matrix."""

import re
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.domain.schemas import CatalogRenderConfig, ResolvedCatalogBranding
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.rendering.catalog_themes import resolve_catalog_theme
from app.scripts.catalog_visual_stress import build_stress_view_model
from app.services.catalog_rendering import render_catalog_html, resolve_catalog_template

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


@pytest.mark.parametrize("theme_key", ["minimal", "premium", "organic", "bold"])
@pytest.mark.parametrize("layout_key", ["classic", "dense", "compact"])
def test_synthetic_theme_layout_pdf_keeps_all_commercial_text(theme_key, layout_key):
    config = CatalogRenderConfig(layout=layout_key, template_key="grabelan-catalog-v2")
    view = build_stress_view_model(config)
    branding = ResolvedCatalogBranding(
        schema_version="catalog-branding-v1", source_profile_id=uuid.uuid4(),
        profile_key="qa-synthetic", display_name=view.branding.display_name,
        primary_color="#596B3F", accent_color="#B08A4A",
    )
    view = view.model_copy(update={"theme": resolve_catalog_theme(theme_key, "1", branding)})
    html = render_catalog_html(view, resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT))
    pdf = ChromiumCatalogPdfRenderer().render(html, config).pdf_bytes
    reader = PdfReader(BytesIO(pdf))
    assert 2 <= len(reader.pages) <= 15
    text = re.sub(r"\s+", " ", " ".join(page.extract_text() or "" for page in reader.pages))
    assert "CATÁLOGO QA - DATOS SINTÉTICOS" in text
    assert "Gs. 999.999.999" in text
    assert "Kombucha sintética" in text
    assert "densidad de tarjetas" in text.casefold()
    if layout_key == "compact":
        assert "Descripción sintética de densidad" not in text
    else:
        assert "Descripción sintética de densidad" in text
