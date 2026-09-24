"""Render synthetic Theme × Layout QA PDFs under ignored tmp/pdfs/."""

import argparse
import json
import uuid
from pathlib import Path

from app.domain.schemas import CatalogRenderConfig, ResolvedCatalogBranding
from app.rendering.catalog_layouts import catalog_layout_definitions
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.rendering.catalog_themes import catalog_theme_definitions, resolve_catalog_theme
from app.scripts.catalog_visual_stress import build_stress_view_model
from app.services.catalog_rendering import render_catalog_html, resolve_catalog_template, validate_catalog_pdf

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "tmp" / "pdfs"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render database-free synthetic catalog Theme samples.")
    parser.add_argument("--theme", choices=[item.key for item in catalog_theme_definitions()] + ["all"], default="all")
    parser.add_argument("--layout", choices=[item.key for item in catalog_layout_definitions()] + ["all"], default="all")
    args = parser.parse_args()
    themes = [item.key for item in catalog_theme_definitions()] if args.theme == "all" else [args.theme]
    layouts = [item.key for item in catalog_layout_definitions()] if args.layout == "all" else [args.layout]
    branding = ResolvedCatalogBranding(
        schema_version="catalog-branding-v1", source_profile_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        profile_key="qa-synthetic", display_name="CATÁLOGO QA - DATOS SINTÉTICOS",
        primary_color="#596B3F", accent_color="#B08A4A",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    renderer = ChromiumCatalogPdfRenderer()
    for layout in layouts:
        config = CatalogRenderConfig(layout=layout, template_key="grabelan-catalog-v2")
        template = resolve_catalog_template(config.template_key)
        for theme in themes:
            view = build_stress_view_model(config).model_copy(update={"theme": resolve_catalog_theme(theme, "1", branding)})
            pdf = renderer.render(render_catalog_html(view, template), config).pdf_bytes
            path = OUTPUT_DIR / f"theme-{theme}-{layout}.pdf"
            path.write_bytes(pdf)
            results.append({"theme": theme, "layout": layout, "pages": validate_catalog_pdf(pdf), "path": str(path)})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
