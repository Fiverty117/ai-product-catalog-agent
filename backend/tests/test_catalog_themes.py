import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.schemas import CatalogBuildCreate, ResolvedCatalogBranding, CatalogRenderConfig
from app.rendering.catalog_themes import (
    InvalidCatalogPaletteError, UnknownCatalogThemeError,
    canonical_color, catalog_theme_definitions, contrast_foreground,
    hash_resolved_catalog_theme, resolve_catalog_theme, resolve_catalog_theme_definition,
)
from app.scripts.catalog_visual_stress import build_stress_view_model
from app.services.catalog_rendering import render_catalog_html, resolve_catalog_template

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


def _branding(primary="#596B3F", accent="#B08A4A"):
    return ResolvedCatalogBranding(
        schema_version="catalog-branding-v1", source_profile_id=uuid.uuid4(),
        profile_key="grabelan", display_name="Grabelan Natural Market",
        primary_color=primary, accent_color=accent,
    )


def test_theme_registry_is_unique_ordered_and_versioned():
    definitions = catalog_theme_definitions()
    assert [(item.key, item.version) for item in definitions] == [
        ("minimal", "1"), ("premium", "1"), ("organic", "1"), ("bold", "1")
    ]
    assert len({(item.key, item.version) for item in definitions}) == 4
    assert resolve_catalog_theme_definition("premium", "1").css_class == "theme-premium"
    with pytest.raises(UnknownCatalogThemeError):
        resolve_catalog_theme_definition("premium", "2")
    with pytest.raises(UnknownCatalogThemeError):
        resolve_catalog_theme_definition("unknown", "1")


def test_theme_resolution_palette_hash_and_contrast_are_deterministic():
    branding = _branding()
    base = resolve_catalog_theme("premium", "1", branding)
    assert (base.primary_color, base.accent_color, base.palette_source) == ("#596B3F", "#B08A4A", "publisher")
    assert base.primary_contrast_color == contrast_foreground("#596B3F")
    assert contrast_foreground("#FFFFFF") == "#000000"
    assert contrast_foreground("#000000") == "#FFFFFF"
    for shade in range(0, 256, 5):
        background = f"#{shade:02X}{shade:02X}{shade:02X}"
        foreground = contrast_foreground(background)
        assert _contrast_ratio(background, foreground) >= 4.5
    assert hash_resolved_catalog_theme(base) == hash_resolved_catalog_theme(resolve_catalog_theme("premium", "1", _branding()))
    primary = resolve_catalog_theme("premium", "1", branding, primary_color_override="#123456")
    accent = resolve_catalog_theme("premium", "1", branding, accent_color_override="#aa0000")
    assert primary.primary_color == "#123456" and primary.accent_color == "#B08A4A"
    assert accent.accent_color == "#AA0000" and accent.primary_color == "#596B3F"
    assert branding.primary_color == "#596B3F" and branding.accent_color == "#B08A4A"
    assert len({hash_resolved_catalog_theme(item) for item in (base, primary, accent, resolve_catalog_theme("organic", "1", branding))}) == 4
    assert canonical_color("#aabbcc") == "#AABBCC"
    for bad in ("red", "#abcd", "rgb(1,2,3)", "#00112233", "#GG1122"):
        with pytest.raises(InvalidCatalogPaletteError):
            canonical_color(bad)
        with pytest.raises(ValidationError):
            CatalogBuildCreate(
                product_ids=[uuid.uuid4()], catalog_brand_profile_id=uuid.uuid4(),
                layout_key="classic", layout_version="1", theme_key="minimal", theme_version="1",
                primary_color_override=bad, idempotency_key="test",
            )


def _contrast_ratio(first: str, second: str) -> float:
    def luminance(color: str) -> float:
        parts = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4 for part in parts]
        return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme_key", ["minimal", "premium", "organic", "bold"])
@pytest.mark.parametrize("layout_key,columns", [("classic", 2), ("dense", 3), ("compact", 4)])
def test_theme_layout_stress_html_preserves_content_and_rows(theme_key, layout_key, columns):
    branding = _branding()
    config = CatalogRenderConfig(layout=layout_key, template_key="grabelan-catalog-v2")
    view = build_stress_view_model(config).model_copy(update={"theme": resolve_catalog_theme(theme_key, "1", branding)})
    template = resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT)
    html = render_catalog_html(view, template)
    assert f"theme-{theme_key}" in html and f"layout-{layout_key}" in html
    assert f'--products-per-row: {columns}' in html
    assert html.count('class="product-card"') == 15
    assert html.count('class="variant-row') == 20
    assert "Gs. 999.999.999" in html
    assert "CATÁLOGO QA - DATOS SINTÉTICOS" in html
    assert "[QA] Densidad de tarjetas" in html
    assert ('class="product-description"' in html) == (layout_key != "compact")
    assert "http://" not in html and "https://" not in html
