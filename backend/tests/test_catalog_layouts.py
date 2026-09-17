import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.schemas import CatalogRenderConfig
from app.rendering.catalog_layouts import (
    UnknownCatalogLayoutError,
    catalog_layout_definitions,
    resolve_catalog_layout,
)
from app.scripts.catalog_visual_stress import (
    build_parser as stress_parser,
    build_stress_view_model,
)
from app.scripts.manual_catalog_render import build_parser as render_parser
from app.services.catalog_rendering import render_catalog_html, resolve_catalog_template

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


def test_layout_registry_has_only_stable_v1_built_ins() -> None:
    definitions = catalog_layout_definitions()

    assert [(item.key, item.version, item.products_per_row) for item in definitions] == [
        ("classic", "1", 2),
        ("dense", "1", 3),
        ("compact", "1", 4),
    ]
    assert all(item.page_size == "A4" for item in definitions)
    assert all(item.orientation == "portrait" for item in definitions)
    assert resolve_catalog_layout("classic").css_class == "layout-classic"
    assert resolve_catalog_layout("dense").css_class == "layout-dense"
    assert resolve_catalog_layout("compact").css_class == "layout-compact"
    with pytest.raises(UnknownCatalogLayoutError, match="unsupported"):
        resolve_catalog_layout("automatic")


def test_render_config_defaults_to_classic_and_rejects_unknown_layout() -> None:
    assert CatalogRenderConfig().layout == "classic"
    assert [CatalogRenderConfig(layout=key).layout for key in ("classic", "dense", "compact")] == [
        "classic",
        "dense",
        "compact",
    ]
    with pytest.raises(ValidationError):
        CatalogRenderConfig(layout="automatic")


@pytest.mark.parametrize(
    ("layout_key", "products_per_row", "expected_counts"),
    [
        ("classic", 2, [2, 2, 2, 1]),
        ("dense", 3, [3, 3, 1]),
        ("compact", 4, [4, 3]),
    ],
)
def test_shared_template_groups_rows_preserves_order_and_emits_layout_class(
    layout_key: str,
    products_per_row: int,
    expected_counts: list[int],
) -> None:
    config = CatalogRenderConfig(layout=layout_key)
    view = build_stress_view_model(config)
    products = view.sections[-1].products[:7]
    section = view.sections[-1].model_copy(update={"products": products})
    view = view.model_copy(update={"sections": [section]})

    html = render_catalog_html(
        view,
        resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT),
    )
    rows = html.split('<div class="product-row">')[1:]

    assert f'class="layout layout-{layout_key}"' in html
    assert f'<main style="--products-per-row: {products_per_row}">' in html
    assert [row.count('class="product-card"') for row in rows] == expected_counts
    names = [product.product_name for product in products]
    assert [html.index(name) for name in names] == sorted(html.index(name) for name in names)
    assert html.count('class="product-card"') == len(products)
    assert html.count('class="variant-row') == sum(len(product.variants) for product in products)


def test_all_layouts_preserve_identical_catalog_and_branding_content() -> None:
    views = {
        key: build_stress_view_model(CatalogRenderConfig(layout=key))
        for key in ("classic", "dense", "compact")
    }
    semantic = {
        key: view.model_dump(mode="json", exclude={"layout"})
        for key, view in views.items()
    }

    assert semantic["classic"] == semantic["dense"] == semantic["compact"]
    for key, view in views.items():
        html = render_catalog_html(
            view,
            resolve_catalog_template(view_model_template_key(), template_root=TEMPLATE_ROOT),
        )
        assert html.count('class="product-card"') == 15
        assert html.count('class="variant-row') == 20
        assert "Gs. 999.999.999" in html
        assert "CATÁLOGO QA - DATOS SINTÉTICOS" in html
        assert f"layout-{key}" in html


def test_descriptions_render_fully_in_classic_and_dense_but_not_compact() -> None:
    rendered = {}
    descriptions = None
    for key in ("classic", "dense", "compact"):
        view = build_stress_view_model(CatalogRenderConfig(layout=key))
        current_descriptions = [
            product.short_description
            for section in view.sections
            for product in section.products
            if product.short_description is not None
        ]
        descriptions = descriptions or current_descriptions
        assert current_descriptions == descriptions
        rendered[key] = render_catalog_html(
            view,
            resolve_catalog_template(
                view_model_template_key(), template_root=TEMPLATE_ROOT
            ),
        )

    assert descriptions
    near_limit = max(descriptions, key=len)
    assert 170 <= len(near_limit) <= 180
    for description in descriptions:
        assert description in rendered["classic"]
        assert description in rendered["dense"]
        assert description not in rendered["compact"]
    assert rendered["classic"].count('class="product-description"') == len(
        descriptions
    )
    assert rendered["dense"].count('class="product-description"') == len(
        descriptions
    )
    assert 'class="product-description"' not in rendered["compact"]

    six_variant_product = build_stress_view_model(
        CatalogRenderConfig(layout="classic")
    ).sections[0].products[0]
    assert len(six_variant_product.variants) == 6
    assert six_variant_product.short_description in rendered["classic"]


def test_layout_css_tunes_dense_and_compact_without_changing_classic_base() -> None:
    stylesheet = (TEMPLATE_ROOT / "catalog-v1.css").read_text(encoding="utf-8")

    assert ".layout-classic" not in stylesheet
    assert ".layout-dense .product-image-frame" in stylesheet
    assert ".layout-dense .product-content h3" in stylesheet
    assert ".layout-dense .variant-price" in stylesheet
    assert ".layout-compact .product-image-frame" in stylesheet
    assert ".layout-compact .product-content h3" in stylesheet
    assert ".layout-compact .variant-row {" in stylesheet
    assert ".layout-compact .variant-price" in stylesheet
    assert "object-fit: contain" in stylesheet


def test_manual_and_stress_cli_layout_parsing() -> None:
    snapshot_id = str(uuid.uuid4())
    manual = render_parser()
    assert manual.parse_args(["--snapshot-id", snapshot_id, "--brand-key", "grabelan"]).layout == "classic"
    assert manual.parse_args(["--snapshot-id", snapshot_id, "--brand-key", "grabelan", "--layout", "dense"]).layout == "dense"
    with pytest.raises(SystemExit):
        manual.parse_args(["--snapshot-id", snapshot_id, "--brand-key", "grabelan", "--layout", "automatic"])

    stress = stress_parser()
    assert stress.parse_args([]).layout == "classic"
    assert stress.parse_args(["--layout", "all"]).layout == "all"
    with pytest.raises(SystemExit):
        stress.parse_args(["--layout", "automatic"])


def view_model_template_key() -> str:
    return "grabelan-catalog-v1"
