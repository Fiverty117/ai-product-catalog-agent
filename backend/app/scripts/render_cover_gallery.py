"""Render database-free synthetic cover QA PDFs under ignored tmp/pdfs/."""

import base64
import hashlib
import json
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from app.domain.schemas import CatalogRenderConfig, FrozenCatalogCoverHero, ResolvedCatalogBranding, ResolvedCatalogCover
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.rendering.catalog_themes import resolve_catalog_theme
from app.scripts.catalog_visual_stress import build_stress_view_model
from app.services.catalog_rendering import render_catalog_html, resolve_catalog_template, validate_catalog_pdf

OUTPUT_DIR = Path(__file__).resolve().parents[3] / "tmp" / "pdfs"


def _synthetic_hero() -> tuple[FrozenCatalogCoverHero, str]:
    image = Image.new("RGB", (1200, 650), "#ECE8DD")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((100, 90, 500, 550), radius=30, fill="#596B3F")
    draw.rounded_rectangle((590, 160, 1050, 500), radius=30, fill="#B08A4A")
    output = BytesIO()
    image.save(output, format="PNG")
    content = output.getvalue()
    checksum = hashlib.sha256(content).hexdigest()
    frozen = FrozenCatalogCoverHero(
        source_cover_asset_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        checksum_sha256=checksum, mime_type="image/png", file_size_bytes=len(content),
        width=1200, height=650, storage_relative_path=f"covers/{checksum[:2]}/{checksum}.png",
    )
    return frozen, "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def _synthetic_logo() -> str:
    image = Image.new("RGBA", (800, 360), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((55, 70, 745, 290), radius=28, fill="#596B3F")
    draw.ellipse((95, 105, 245, 255), fill="#B08A4A")
    draw.rounded_rectangle((300, 120, 670, 150), radius=12, fill="#F8F5EF")
    draw.rounded_rectangle((300, 182, 590, 207), radius=10, fill="#F8F5EF")
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    branding = ResolvedCatalogBranding(
        schema_version="catalog-branding-v1", source_profile_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        profile_key="qa-synthetic", display_name="CATÁLOGO QA - DATOS SINTÉTICOS",
        primary_color="#596B3F", accent_color="#B08A4A",
    )
    hero, hero_uri = _synthetic_hero()
    logo_uri = _synthetic_logo()
    renderer = ChromiumCatalogPdfRenderer()
    results = []
    for name, cover_key, theme_key, layout_key, copy_length, with_logo in (
        ("premium-classic-no-cover", None, "premium", "classic", "short", False),
        ("premium-classic-minimal", "minimal", "premium", "classic", "short", False),
        ("premium-classic-editorial", "editorial", "premium", "classic", "short", False),
        ("minimal-classic-minimal", "minimal", "minimal", "classic", "short", False),
        ("organic-classic-editorial", "editorial", "organic", "classic", "short", False),
        ("premium-dense-editorial", "editorial", "premium", "dense", "short", False),
        ("premium-compact-minimal", "minimal", "premium", "compact", "short", False),
        ("premium-classic-editorial-logo", "editorial", "premium", "classic", "short", True),
        ("premium-classic-minimal-medium", "minimal", "premium", "classic", "medium", False),
        ("premium-classic-editorial-long", "editorial", "premium", "classic", "long", False),
        ("bold-classic-hero", "hero", "bold", "classic", "short", False),
    ):
        config = CatalogRenderConfig(layout=layout_key, template_key="grabelan-catalog-v3")
        cover = ResolvedCatalogCover(
            schema_version="catalog-cover-v1", enabled=cover_key is not None,
            cover_key=cover_key, cover_version="1" if cover_key else None,
            title=(
                "Catálogo mayorista de bienestar y productos naturales para tiendas locales 2026."
                if copy_length == "long" else "Catálogo Natural 2026" if copy_length == "medium" else "Catálogo"
            ) if cover_key else None,
            subtitle=(
                "Selección de productos con descripciones extensas para comprobar la lectura en un diseño editorial de portada, incluso cuando el texto ocupa varias líneas sin invadir otras zonas."
                if copy_length == "long" else "Selección natural para cada día"
            ) if cover_key else None,
            edition_label=(
                "Edición mayorista para septiembre de dos mil veintiséis"
                if copy_length == "long" else "Edición 2026"
            ) if cover_key else None,
            show_publisher_logo=with_logo,
            hero=hero if cover_key == "hero" else None,
        )
        base_view = build_stress_view_model(config)
        view = base_view.model_copy(update={
            "theme": resolve_catalog_theme(theme_key, "1", branding),
            "cover": cover, "cover_hero_data_uri": hero_uri if cover_key == "hero" else None,
            "branding": base_view.branding.model_copy(update={"logo_data_uri": logo_uri}) if with_logo else base_view.branding,
        })
        pdf = renderer.render(render_catalog_html(view, resolve_catalog_template(config.template_key)), config).pdf_bytes
        path = OUTPUT_DIR / f"12b1-{name}.pdf"
        path.write_bytes(pdf)
        results.append({"case": name, "cover": cover_key, "theme": theme_key, "layout": layout_key,
            "pages": validate_catalog_pdf(pdf), "path": str(path)})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
