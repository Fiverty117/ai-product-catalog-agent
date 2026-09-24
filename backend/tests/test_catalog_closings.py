"""Focused v5 closing contract checks; no live Catalog Build or external services."""

import base64
import uuid

import pytest
from pydantic import ValidationError

from app.domain.schemas import CatalogClosingCreate, ResolvedCatalogBranding, ResolvedCatalogClosing
from app.rendering.catalog_closings import (
    CatalogClosingError, UnknownCatalogClosingError, catalog_closing_definitions,
    closing_qr_data_uri, hash_resolved_catalog_closing, resolve_catalog_closing,
    resolve_catalog_closing_definition,
)


def _branding(**changes):
    data = dict(schema_version="catalog-branding-v1", source_profile_id=uuid.uuid4(),
                profile_key="publisher", display_name="Publisher",
                primary_color="#123456", accent_color="#ABCDEF",
                contact_text="Contacto base", social_handle="@base")
    data.update(changes)
    return ResolvedCatalogBranding(**data)


def _choice(**changes):
    data = dict(enabled=True, closing_key="contact", closing_version="1", heading="Hacé tu pedido")
    data.update(changes)
    return CatalogClosingCreate(**data)


def test_closing_registry_disabled_choice_and_frozen_hash():
    assert [item.key for item in catalog_closing_definitions()] == ["minimal", "contact", "order"]
    with pytest.raises(UnknownCatalogClosingError):
        resolve_catalog_closing_definition("order", "2")
    disabled = resolve_catalog_closing(CatalogClosingCreate(enabled=False), _branding())
    assert disabled.enabled is False and disabled.contacts == []
    assert disabled.qr_target_url is None
    with pytest.raises(ValidationError):
        CatalogClosingCreate(enabled=False, heading="ignored")
    first = resolve_catalog_closing(_choice(), _branding())
    second = resolve_catalog_closing(_choice(note="Otra edición"), _branding())
    assert hash_resolved_catalog_closing(first) != hash_resolved_catalog_closing(second)
    assert hash_resolved_catalog_closing(first) == hash_resolved_catalog_closing(ResolvedCatalogClosing.model_validate(first.model_dump()))


def test_contacts_resolve_from_frozen_profile_or_build_override_with_safe_links():
    choice = _choice(
        publisher_contact={"enabled": True}, publisher_social={"enabled": True, "override": "@edición"},
        whatsapp={"enabled": True, "override": "+595 981 123456"},
        phone={"enabled": True, "override": "021 123456"},
        instagram={"enabled": True, "override": "@tienda.py"},
        website={"enabled": True, "override": "https://example.com/pedidos"},
        address={"enabled": True, "override": "Asunción, Paraguay"},
        qr={"enabled": True, "target_type": "whatsapp"},
    )
    closing = resolve_catalog_closing(choice, _branding())
    values = {item.kind: item for item in closing.contacts}
    assert values["publisher_contact"].value == "Contacto base" and values["publisher_contact"].href is None
    assert values["publisher_social"].value == "@edición" and values["publisher_social"].href is None
    assert values["whatsapp"].href == "https://wa.me/595981123456"
    assert values["phone"].href == "tel:021123456"
    assert values["instagram"].href == "https://www.instagram.com/tienda.py/"
    assert values["website"].href == "https://example.com/pedidos"
    assert values["address"].href is None
    assert closing.qr_target_url == values["whatsapp"].href
    qr = closing_qr_data_uri(closing)
    assert qr and base64.b64decode(qr.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n")
    assert qr == closing_qr_data_uri(closing)


@pytest.mark.parametrize("values", [
    {"closing_key": "order"},
    {"website": {"enabled": True, "override": "javascript:alert(1)"}},
    {"website": {"enabled": True, "override": "file:///etc/passwd"}},
    {"whatsapp": {"enabled": True, "override": "0981 123456"}, "qr": {"enabled": True, "target_type": "whatsapp"}},
    {"qr": {"enabled": True, "target_type": "website"}},
])
def test_invalid_or_incomplete_closing_fails_closed(values):
    with pytest.raises(CatalogClosingError):
        resolve_catalog_closing(_choice(**values), _branding())


def test_frozen_links_and_qr_cannot_be_replaced_with_unsafe_destinations():
    closing = resolve_catalog_closing(_choice(website={"enabled": True, "override": "https://example.com"},
                                              qr={"enabled": True, "target_type": "website"}), _branding())
    data = closing.model_dump()
    data["contacts"][0]["href"] = "javascript:alert(1)"
    with pytest.raises(ValidationError):
        ResolvedCatalogClosing.model_validate(data)
    data = closing.model_dump()
    data["qr_target_url"] = "https://other.example/"
    with pytest.raises(ValidationError):
        ResolvedCatalogClosing.model_validate(data)


def test_hash_tracks_visible_choices_but_not_publisher_database_id():
    branding = _branding()
    choice = _choice(publisher_contact={"enabled": True})
    baseline = resolve_catalog_closing(choice, branding)
    assert hash_resolved_catalog_closing(baseline) == hash_resolved_catalog_closing(
        resolve_catalog_closing(choice, _branding()))
    variants = [
        _choice(closing_key="minimal", publisher_contact={"enabled": True}),
        _choice(heading="Otro título", publisher_contact={"enabled": True}),
        _choice(note="Una nota", publisher_contact={"enabled": True}),
        _choice(publisher_contact={"enabled": True, "override": "Contacto alternativo"}),
        _choice(),
        _choice(publisher_contact={"enabled": True}, qr={"enabled": True, "target_type": "custom_url", "custom_url": "https://example.com/uno"}),
    ]
    hashes = {hash_resolved_catalog_closing(resolve_catalog_closing(item, branding)) for item in variants}
    assert len(hashes) == len(variants)
    assert hash_resolved_catalog_closing(baseline) not in hashes


def test_website_and_custom_qr_targets_are_explicit_and_safe():
    website = resolve_catalog_closing(_choice(
        website={"enabled": True, "override": "https://example.com/orders"},
        qr={"enabled": True, "target_type": "website"}), _branding())
    assert website.qr_target_url == "https://example.com/orders"
    custom = resolve_catalog_closing(_choice(
        qr={"enabled": True, "target_type": "custom_url", "custom_url": "https://example.org/order"}), _branding())
    assert custom.qr_target_url == "https://example.org/order"
    assert hash_resolved_catalog_closing(website) != hash_resolved_catalog_closing(custom)
    for url in ("javascript:alert(1)", "file:///tmp/x", "data:text/plain,hello", "https://user:pass@example.com/"):
        with pytest.raises(CatalogClosingError):
            resolve_catalog_closing(_choice(qr={"enabled": True, "target_type": "custom_url", "custom_url": url}), _branding())


def test_one_page_print_budget_rejects_excessive_text():
    with pytest.raises(CatalogClosingError, match="one-page print budget"):
        resolve_catalog_closing(_choice(
            heading="H" * 80, note="N" * 300,
            publisher_contact={"enabled": True, "override": "C" * 500},
            address={"enabled": True, "override": "A" * 80}), _branding())
