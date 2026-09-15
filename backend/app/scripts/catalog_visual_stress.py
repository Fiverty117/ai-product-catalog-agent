import base64
import json
import uuid
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw

from app.domain.schemas import (
    CatalogRenderConfig,
    CatalogRenderProductView,
    CatalogRenderSectionView,
    CatalogRenderVariantView,
    CatalogRenderViewModel,
)
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.services.catalog_rendering import (
    render_catalog_html,
    resolve_catalog_template,
    validate_catalog_pdf,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "tmp" / "catalog-visual-stress"
HTML_FILENAME = "catalog-v1-stress.html"
PDF_FILENAME = "catalog-v1-stress.pdf"
QA_NAMESPACE = uuid.UUID("4ecad6a8-7451-4219-a670-7883423e758c")


def main() -> None:
    config = CatalogRenderConfig()
    template = resolve_catalog_template(config.template_key)
    view_model = build_stress_view_model(config)
    html = render_catalog_html(view_model, template)
    rendered = ChromiumCatalogPdfRenderer().render(html, config)
    page_count = validate_catalog_pdf(rendered.pdf_bytes)
    if page_count < 2:
        raise RuntimeError("stress preview must span at least two A4 pages")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = (OUTPUT_DIR / HTML_FILENAME).resolve()
    pdf_path = (OUTPUT_DIR / PDF_FILENAME).resolve()
    html_path.write_text(html, encoding="utf-8")
    pdf_path.write_bytes(rendered.pdf_bytes)
    print(
        json.dumps(
            {
                "html_path": str(html_path),
                "pdf_path": str(pdf_path),
                "page_count": page_count,
                "renderer_engine": rendered.engine,
                "renderer_engine_version": rendered.engine_version,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def build_stress_view_model(
    config: CatalogRenderConfig | None = None,
) -> CatalogRenderViewModel:
    resolved_config = config or CatalogRenderConfig()
    two_product_section = _section(
        "two-products",
        "[QA] Categoría de prueba con un nombre deliberadamente largo para revisar saltos de línea",
        [
            _product(
                "many-variants",
                brand=(
                    "[QA] Cooperativa Internacional de Productores Naturales "
                    "de Nombre Deliberadamente Extenso"
                ),
                name=(
                    "[QA] Producto de prueba con proteína vegetal fermentada, "
                    "ingredientes cultivados y un nombre comercial muy largo"
                ),
                image_shape="portrait",
                variants=[
                    ("Vainilla clásica / 500 g", "Gs. 180.000"),
                    ("Chocolate intenso / 500 g", "Gs. 185.000"),
                    ("Frutilla natural / 1 kg", "Gs. 320.000"),
                    (
                        "Maracuyá, jengibre y mezcla botánica con una descripción de variante deliberadamente extensa / 2 kg",
                        "Gs. 540.000",
                    ),
                    ("Sin sabor / presentación familiar", "Gs. 610.000"),
                    ("Edición QA de precio grande / 10 kg", "Gs. 999.999.999"),
                ],
            ),
            _product(
                "square-companion",
                brand="[QA] Marca cuadrada de prueba",
                name="[QA] Producto de prueba en envase cuadrado",
                image_shape="square",
                variants=[("Natural / 250 g", "Gs. 72.500")],
            ),
        ],
    )

    four_product_sections = [
        _section(
            "four-products-a",
            "[QA] Despensa funcional - grupo A",
            [
                _product(
                    "flour",
                    brand="[QA] Molino de prueba",
                    name="[QA] Harina integral de prueba",
                    image_shape="portrait",
                    variants=[("Bolsa / 1 kg", "Gs. 18.900")],
                ),
                _product(
                    "fruit",
                    brand="[QA] Frutos de laboratorio visual",
                    name="[QA] Fruta deshidratada de prueba",
                    image_shape="square",
                    variants=[("Mix tropical / 350 g", "Gs. 44.500")],
                ),
            ],
        ),
        _section(
            "four-products-b",
            "[QA] Refrigerados y bebidas - grupo B",
            [
                _product(
                    "kombucha",
                    brand="[QA] Fermentos de prueba",
                    name="[QA] Kombucha sintética para control de diseño",
                    image_shape="portrait",
                    variants=[("Jengibre / 330 ml", "Gs. 16.000")],
                ),
                _product(
                    "yogurt",
                    brand="[QA] Lácteos de maqueta",
                    name="[QA] Yogur natural de prueba",
                    image_shape="square",
                    variants=[("Natural / 500 g", "Gs. 24.000")],
                ),
            ],
        ),
    ]

    density_products = [
        _product(
            f"density-{number}",
            brand=f"[QA] Marca sintética {number}",
            name=f"[QA] Producto de prueba para densidad y paginación {number}",
            image_shape="portrait" if number % 2 else "square",
            variants=[
                (
                    f"Variante visual {number} / {number * 100} g",
                    f"Gs. {number * 37}.000",
                )
            ],
        )
        for number in range(1, 10)
    ]
    density_section = _section(
        "page-density",
        "[QA] Densidad de tarjetas para forzar paginación A4",
        density_products,
    )

    return CatalogRenderViewModel(
        locale=resolved_config.locale,
        page_size=resolved_config.page_size,
        orientation=resolved_config.orientation,
        store_name="CATÁLOGO QA - DATOS SINTÉTICOS",
        title="Prueba visual Classic",
        as_of_label="14 de septiembre de 2026",
        currency="PYG",
        sections=[
            two_product_section,
            *four_product_sections,
            density_section,
        ],
    )


def _section(
    key: str,
    name: str,
    products: list[CatalogRenderProductView],
) -> CatalogRenderSectionView:
    return CatalogRenderSectionView(
        source_category_id=_qa_uuid(f"category:{key}"),
        category_name=name,
        products=products,
    )


def _product(
    key: str,
    *,
    brand: str,
    name: str,
    image_shape: Literal["portrait", "square"],
    variants: list[tuple[str | None, str]],
) -> CatalogRenderProductView:
    return CatalogRenderProductView(
        source_product_id=_qa_uuid(f"product:{key}"),
        brand_name=brand,
        product_name=name,
        image_data_uri=_fixture_image_data_uri(image_shape, key),
        variants=[
            CatalogRenderVariantView(
                source_sku_id=_qa_uuid(f"sku:{key}:{index}"),
                label=label,
                price_display=price,
            )
            for index, (label, price) in enumerate(variants, start=1)
        ],
    )


def _fixture_image_data_uri(
    shape: Literal["portrait", "square"],
    key: str,
) -> str:
    width, height = (420, 720) if shape == "portrait" else (600, 600)
    image = Image.new("RGB", (width, height), "#f1eee8")
    draw = ImageDraw.Draw(image)
    inset = max(30, width // 10)
    draw.rounded_rectangle(
        (inset, inset, width - inset, height - inset),
        radius=max(18, width // 18),
        fill="#d8c8b2",
        outline="#87663f",
        width=max(4, width // 100),
    )
    label = f"QA {shape.upper()}\n{key.upper()}"
    box = draw.multiline_textbbox((0, 0), label, spacing=8, align="center")
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]
    draw.multiline_text(
        ((width - text_width) / 2, (height - text_height) / 2),
        label,
        fill="#27241f",
        spacing=8,
        align="center",
    )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _qa_uuid(key: str) -> uuid.UUID:
    return uuid.uuid5(QA_NAMESPACE, key)


if __name__ == "__main__":
    main()
