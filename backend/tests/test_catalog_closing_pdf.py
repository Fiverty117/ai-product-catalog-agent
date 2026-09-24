"""Synthetic offline PDF checks for the independently versioned closing page."""

import base64
import re
import shutil
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from app.domain.schemas import CatalogClosingCreate, CatalogRenderConfig, ResolvedCatalogBranding, ResolvedCatalogCover
from app.rendering.catalog_closings import closing_qr_data_uri, resolve_catalog_closing
from app.rendering.catalog_pdf import ChromiumCatalogPdfRenderer
from app.rendering.catalog_themes import resolve_catalog_theme
from app.scripts.catalog_visual_stress import build_stress_view_model
from app.services.catalog_rendering import hash_catalog_template, render_catalog_html, resolve_catalog_template

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


def _view(layout: str, theme: str, choice: CatalogClosingCreate):
    config = CatalogRenderConfig(layout=layout, template_key="grabelan-catalog-v4")
    branding = ResolvedCatalogBranding(
        schema_version="catalog-branding-v1", source_profile_id=uuid.uuid4(),
        profile_key="synthetic", display_name="EDITORIAL QA SINTÉTICA",
        primary_color="#596B3F", accent_color="#B08A4A",
        contact_text="Atención comercial", social_handle="@qa.editorial",
    )
    closing = resolve_catalog_closing(choice, branding)
    view = build_stress_view_model(config).model_copy(update={
        "theme": resolve_catalog_theme(theme, "1", branding),
        "cover": ResolvedCatalogCover(schema_version="catalog-cover-v1", enabled=False),
        "closing": closing,
        "closing_qr_data_uri": closing_qr_data_uri(closing),
    })
    return config, view


def _render(config, view):
    html = render_catalog_html(view, resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT))
    return html, ChromiumCatalogPdfRenderer().render(html, config).pdf_bytes


@pytest.mark.parametrize("layout,theme,style", [
    ("classic", "minimal", "minimal"),
    ("dense", "premium", "contact"),
    ("compact", "bold", "order"),
])
def test_closing_adds_exactly_one_last_page_without_changing_product_text(layout, theme, style, tmp_path):
    base = dict(enabled=True, closing_key=style, closing_version="1", heading="Hacé tu pedido",
                note="Escribinos para confirmar disponibilidad y entrega.",
                publisher_contact={"enabled": True},
                whatsapp={"enabled": True, "override": "+595 981 123456"},
                website={"enabled": True, "override": "https://example.com/pedidos"},
                qr={"enabled": True, "target_type": "website"})
    config, view = _view(layout, theme, CatalogClosingCreate(**base))
    html, pdf = _render(config, view)
    assert 'href="https://example.com/pedidos"' in html
    assert 'href="https://wa.me/595981123456"' in html
    reader = PdfReader(BytesIO(pdf))
    disabled_config, disabled_view = _view(layout, theme, CatalogClosingCreate(enabled=False))
    _, disabled_pdf = _render(disabled_config, disabled_view)
    disabled_reader = PdfReader(BytesIO(disabled_pdf))
    assert len(reader.pages) == len(disabled_reader.pages) + 1
    for page, original in zip(reader.pages[:-1], disabled_reader.pages):
        assert page.extract_text() == original.extract_text()
    last = re.sub(r"\s+", " ", reader.pages[-1].extract_text() or "")
    assert "Hacé tu pedido" in last and "Atención comercial" in last
    assert "Kombucha sintética" not in last
    links = {annotation.get_object().get("/A", {}).get("/URI")
             for annotation in (reader.pages[-1].get("/Annots") or [])}
    assert {"https://wa.me/595981123456", "https://example.com/pedidos"} <= links
    assert "Gs. 999.999.999" in " ".join(page.extract_text() or "" for page in reader.pages[:-1])
    if style == "order":
        (tmp_path / "closing-order.pdf").write_bytes(pdf)


def test_disabled_v5_matches_v4_and_markup_is_escaped():
    config, view = _view("classic", "organic", CatalogClosingCreate(enabled=False))
    html, pdf = _render(config, view)
    assert "catalog-closing" in html  # stylesheet exists, but no page element
    assert '<section class="catalog-closing' not in html
    v4_config = CatalogRenderConfig(layout="classic", template_key="grabelan-catalog-v3")
    v4_view = view.model_copy(update={"closing": None, "closing_qr_data_uri": None})
    _, old_pdf = _render(v4_config, v4_view)
    current, old = PdfReader(BytesIO(pdf)), PdfReader(BytesIO(old_pdf))
    assert len(current.pages) == len(old.pages)
    assert [page.extract_text() for page in current.pages] == [page.extract_text() for page in old.pages]

    choice = CatalogClosingCreate(enabled=True, closing_key="minimal", closing_version="1", heading="Valid title",
                                  publisher_contact={"enabled": True, "override": "A & B ©"})
    _, custom_view = _view("classic", "organic", choice)
    custom_html, _ = _render(config, custom_view)
    assert "A &amp; B ©" in custom_html


def test_cover_and_closing_add_two_pages_in_correct_order():
    config, base = _view("classic", "premium", CatalogClosingCreate(enabled=False))
    _, base_pdf = _render(config, base)
    cover = ResolvedCatalogCover(schema_version="catalog-cover-v1", enabled=True,
                                 cover_key="editorial", cover_version="1", title="Edición sintética")
    _, cover_pdf = _render(config, base.model_copy(update={"cover": cover}))
    choice = CatalogClosingCreate(enabled=True, closing_key="minimal", closing_version="1",
                                  publisher_contact={"enabled": True})
    _, closing_view = _view("classic", "premium", choice)
    _, closing_pdf = _render(config, closing_view)
    _, both_pdf = _render(config, closing_view.model_copy(update={"cover": cover}))
    readers = [PdfReader(BytesIO(data)) for data in (base_pdf, cover_pdf, closing_pdf, both_pdf)]
    count = len(readers[0].pages)
    assert [len(item.pages) for item in readers] == [count, count + 1, count + 1, count + 2]
    assert "Edición sintética" in (readers[3].pages[0].extract_text() or "")
    assert "Kombucha sintética" in " ".join(page.extract_text() or "" for page in readers[3].pages[1:-1])
    assert "Atención comercial" in (readers[3].pages[-1].extract_text() or "")


def test_long_closing_content_stays_on_one_page(tmp_path):
    choice = CatalogClosingCreate(
        enabled=True, closing_key="contact", closing_version="1",
        heading="Información para pedidos y consultas sobre todos nuestros productos de temporada",
        note="Consultá disponibilidad y condiciones de entrega antes de confirmar el pedido. " * 3,
        publisher_contact={"enabled": True, "override": "Departamento de atención comercial y pedidos"},
        publisher_social={"enabled": True, "override": "@publicacion.edicion.2026"},
        whatsapp={"enabled": True, "override": "+595 981 123456"},
        phone={"enabled": True, "override": "+595 21 123456"},
        instagram={"enabled": True, "override": "@" + "a" * 30},
        website={"enabled": True, "override": "https://example.com/" + "catalogo/" * 17 + "pedidos"},
        address={"enabled": True, "override": "Asunción, Paraguay, local de atención comercial, edificio principal, " * 3},
        qr={"enabled": True, "target_type": "website"},
    )
    config, view = _view("compact", "organic", choice)
    _, pdf = _render(config, view)
    (tmp_path / "closing-long.pdf").write_bytes(pdf)
    reader = PdfReader(BytesIO(pdf))
    _, plain_view = _view("compact", "organic", CatalogClosingCreate(enabled=False))
    _, plain_pdf = _render(config, plain_view)
    assert len(reader.pages) == len(PdfReader(BytesIO(plain_pdf)).pages) + 1
    last = reader.pages[-1].extract_text() or ""
    for fragment in ("pedidos", "Departamento", "local de", "Sitio web"):
        assert fragment in last


@pytest.mark.parametrize("case,theme,style,contacts,qr,logo", [
    ("a-order-phone-no-qr", "premium", "order", {"phone": {"enabled": True, "override": "0982 900 806"}}, None, False),
    ("b-order-phone-website-qr", "premium", "order", {"phone": {"enabled": True, "override": "0982 900 806"}, "website": {"enabled": True, "override": "https://example.com/pedidos"}}, "website", False),
    ("c-contact-multiple-no-qr", "premium", "contact", {"phone": {"enabled": True, "override": "+595 982 900 806"}, "instagram": {"enabled": True, "override": "@ejemplo"}, "website": {"enabled": True, "override": "https://example.com/pedidos"}, "address": {"enabled": True, "override": "Asunción, Paraguay"}}, None, True),
    ("d-contact-multiple-qr", "premium", "contact", {"phone": {"enabled": True, "override": "+595 982 900 806"}, "instagram": {"enabled": True, "override": "@ejemplo"}, "website": {"enabled": True, "override": "https://example.com/pedidos"}, "address": {"enabled": True, "override": "Asunción, Paraguay"}}, "website", True),
    ("e-organic-phone-no-qr", "organic", "contact", {"phone": {"enabled": True, "override": "0982 900 806"}}, None, False),
    ("f-bold-order-website-qr", "bold", "order", {"website": {"enabled": True, "override": "https://example.com/pedidos"}}, "website", False),
], ids=lambda value: value if isinstance(value, str) and value.startswith(("a-", "b-", "c-", "d-", "e-", "f-")) else None)
def test_closing_polish_visual_states(case, theme, style, contacts, qr, logo, tmp_path):
    choice = CatalogClosingCreate(
        enabled=True, closing_key=style, closing_version="1", heading="Hacé tu pedido",
        **contacts, **({"qr": {"enabled": True, "target_type": qr}} if qr else {}),
    )
    config, view = _view("classic", theme, choice)
    if logo:
        output = BytesIO()
        Image.new("RGB", (500, 170), (89, 107, 63)).save(output, format="PNG")
        logo_uri = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        view = view.model_copy(update={
            "branding": view.branding.model_copy(update={"logo_data_uri": logo_uri}),
            "closing": view.closing.model_copy(update={"show_publisher_logo": True}),
        })
    html, pdf = _render(config, view)
    assert "Hacé tu pedido" in html
    assert ("closing--with-qr" if qr else "closing--without-qr") in html
    assert ("closing--with-logo" if logo else "closing--without-logo") in html
    assert ("closing--single-contact" if len(contacts) == 1 else "closing--multiple-contacts") in html
    assert html.count('class="closing-contact-item') == len(contacts)
    assert ('class="closing-qr"' in html) is bool(qr)
    if "phone" in contacts:
        assert contacts["phone"]["override"] in html
        assert 'href="tel:' + ("+" if contacts["phone"]["override"].startswith("+") else "") + "".join(
            char for char in contacts["phone"]["override"] if char.isdigit()) + '"' in html
    if qr == "website":
        assert '<span class="closing-qr-label">Sitio web</span>' in html
    reader = PdfReader(BytesIO(pdf))
    assert "Hacé tu pedido" in (reader.pages[-1].extract_text() or "")
    assert "Kombucha sintética" not in (reader.pages[-1].extract_text() or "")
    if qr:
        encoded = view.closing_qr_data_uri.split(",", 1)[1]
        original_qr = Image.open(BytesIO(base64.b64decode(encoded))).convert("RGB")
        embedded = [Image.open(BytesIO(item.data)).convert("RGB") for item in reader.pages[-1].images]
        assert any(image.size == original_qr.size and image.tobytes() == original_qr.tobytes() for image in embedded)
        assert original_qr.width == original_qr.height
        assert original_qr.getpixel((31, 31)) == (255, 255, 255)
        assert original_qr.getpixel((32, 32)) == (17, 17, 17)
    (tmp_path / f"{case}.pdf").write_bytes(pdf)


@pytest.mark.parametrize("target,expected", [
    ("whatsapp", "WhatsApp"), ("website", "Sitio web"), ("custom_url", "Enlace"),
])
def test_qr_caption_uses_frozen_destination_type_without_changing_target(target, expected):
    choice = CatalogClosingCreate(
        enabled=True, closing_key="order", closing_version="1", heading="Pedidos",
        whatsapp={"enabled": True, "override": "+595 982 900 806"},
        website={"enabled": True, "override": "https://example.com/pedidos"},
        qr={"enabled": True, "target_type": target,
            **({"custom_url": "https://example.org/consultas"} if target == "custom_url" else {})},
    )
    config, view = _view("classic", "premium", choice)
    html = render_catalog_html(view, resolve_catalog_template(config.template_key, template_root=TEMPLATE_ROOT))
    assert f'<span class="closing-qr-label">{expected}</span>' in html
    if target == "custom_url":
        assert view.closing.qr_target_url in html
    else:
        assert view.closing.qr_target_url in {item.href for item in view.closing.contacts}


def test_v5_template_hash_tracks_closing_files_without_changing_old_hashes(tmp_path):
    copied = tmp_path / "templates"
    shutil.copytree(TEMPLATE_ROOT, copied)
    keys = ["grabelan-catalog-v1", "grabelan-catalog-v2", "grabelan-catalog-v3", "grabelan-catalog-v4"]
    templates = [resolve_catalog_template(key, template_root=copied) for key in keys]
    original = [hash_catalog_template(template) for template in templates]
    previous_v5 = original[3]
    for filename in ("catalog-v4.html.jinja", "catalog-v4-closing.css"):
        target = copied / filename
        target.write_text(target.read_text(encoding="utf-8") + "\n/* synthetic hash check */\n", encoding="utf-8")
        changed = [hash_catalog_template(template) for template in templates]
        assert changed[:3] == original[:3]
        assert changed[3] != previous_v5
        previous_v5 = changed[3]
