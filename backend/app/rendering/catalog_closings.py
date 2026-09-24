"""Versioned closing compositions, safe contact resolution and local QR data."""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from app.domain.schemas import (
    CatalogClosingCreate,
    ResolvedCatalogBranding,
    ResolvedCatalogClosing,
    ResolvedCatalogClosingContact,
)

CLOSING_SCHEMA_VERSION = "catalog-closing-v1"
QR_RENDER_VERSION = "qr-png-m-v1"
_PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9(). -]*$")
_INSTAGRAM_PATTERN = re.compile(r"^@?[A-Za-z0-9._]{1,30}$")


class CatalogClosingError(ValueError):
    pass


class UnknownCatalogClosingError(CatalogClosingError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogClosingDefinition:
    key: str
    version: str
    display_name: str
    description: str
    css_class: str
    requires_contact: bool


_CLOSINGS = (
    CatalogClosingDefinition("minimal", "1", "Minimal", "Quiet publisher sign-off", "closing-minimal", False),
    CatalogClosingDefinition("contact", "1", "Contact", "Structured contact information", "closing-contact", False),
    CatalogClosingDefinition("order", "1", "Order", "Ordering-focused final page", "closing-order", True),
)


def catalog_closing_definitions() -> tuple[CatalogClosingDefinition, ...]:
    return _CLOSINGS


def resolve_catalog_closing_definition(key: str, version: str) -> CatalogClosingDefinition:
    for item in _CLOSINGS:
        if item.key == key and item.version == version:
            return item
    raise UnknownCatalogClosingError(f"unsupported catalog closing: {key}/{version}")


def _plain(value: str, *, maximum: int, label: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum or "<" in normalized or ">" in normalized or any(ord(char) < 32 for char in normalized):
        raise CatalogClosingError(f"invalid {label}")
    return normalized


def _phone(value: str, *, label: str) -> tuple[str, str | None]:
    display = _plain(value, maximum=40, label=label)
    if not _PHONE_PATTERN.fullmatch(display):
        raise CatalogClosingError(f"invalid {label}")
    digits = "".join(char for char in display if char.isdigit())
    if not 4 <= len(digits) <= 20:
        raise CatalogClosingError(f"invalid {label}")
    return display, ("tel:" + ("+" if display.startswith("+") else "") + digits)


def whatsapp_url(value: str) -> str | None:
    display, _ = _phone(value, label="WhatsApp number")
    # A local-looking number is printable but cannot safely become a wa.me URL.
    if not display.startswith("+"):
        return None
    return "https://wa.me/" + "".join(char for char in display if char.isdigit())


def safe_http_url(value: str, *, label: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 255 or any(char.isspace() or ord(char) < 32 for char in candidate) or "\\" in candidate or "<" in candidate or ">" in candidate:
        raise CatalogClosingError(f"invalid {label}")
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        raise CatalogClosingError(f"invalid {label}") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password or port == 0:
        raise CatalogClosingError(f"invalid {label}")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))


def _instagram(value: str) -> tuple[str, str]:
    display = _plain(value, maximum=31, label="Instagram handle")
    if not _INSTAGRAM_PATTERN.fullmatch(display):
        raise CatalogClosingError("invalid Instagram handle")
    handle = display.lstrip("@").lower()
    return "@" + handle, "https://www.instagram.com/" + handle + "/"


def resolve_catalog_closing(choice: CatalogClosingCreate, branding: ResolvedCatalogBranding) -> ResolvedCatalogClosing:
    if not choice.enabled:
        return ResolvedCatalogClosing(schema_version=CLOSING_SCHEMA_VERSION, enabled=False)
    definition = resolve_catalog_closing_definition(choice.closing_key or "", choice.closing_version or "")
    contacts: list[ResolvedCatalogClosingContact] = []

    def add(kind: str, value: str | None, *, maximum: int, href: str | None = None) -> None:
        if value is None:
            raise CatalogClosingError(f"{kind} is enabled but has no value")
        contacts.append(ResolvedCatalogClosingContact(kind=kind, value=_plain(value, maximum=maximum, label=kind), href=href))

    if choice.publisher_contact.enabled:
        add("publisher_contact", choice.publisher_contact.override or branding.contact_text, maximum=500)
    if choice.publisher_social.enabled:
        add("publisher_social", choice.publisher_social.override or branding.social_handle, maximum=255)
    if choice.whatsapp.enabled:
        if not choice.whatsapp.override:
            raise CatalogClosingError("WhatsApp is enabled but has no value")
        display, _ = _phone(choice.whatsapp.override, label="WhatsApp number")
        add("whatsapp", display, maximum=40, href=whatsapp_url(display))
    if choice.phone.enabled:
        if not choice.phone.override:
            raise CatalogClosingError("phone is enabled but has no value")
        display, href = _phone(choice.phone.override, label="phone number")
        add("phone", display, maximum=40, href=href)
    if choice.instagram.enabled:
        if not choice.instagram.override:
            raise CatalogClosingError("Instagram is enabled but has no value")
        display, href = _instagram(choice.instagram.override)
        add("instagram", display, maximum=31, href=href)
    if choice.website.enabled:
        if not choice.website.override:
            raise CatalogClosingError("website is enabled but has no value")
        url = safe_http_url(choice.website.override, label="website URL")
        add("website", url, maximum=255, href=url)
    if choice.address.enabled:
        add("address", choice.address.override, maximum=240)

    qr_type = choice.qr.target_type if choice.qr.enabled else None
    qr_url: str | None = None
    if qr_type == "whatsapp":
        contact = next((item for item in contacts if item.kind == "whatsapp"), None)
        qr_url = contact.href if contact else None
    elif qr_type == "website":
        contact = next((item for item in contacts if item.kind == "website"), None)
        qr_url = contact.href if contact else None
    elif qr_type == "custom_url":
        qr_url = safe_http_url(choice.qr.custom_url or "", label="custom QR URL")
    if choice.qr.enabled and qr_url is None:
        raise CatalogClosingError("QR target requires a usable visible contact value")
    if definition.requires_contact and not contacts:
        raise CatalogClosingError("order closing requires a contact method")
    if not (choice.heading or choice.note or contacts or qr_url):
        raise CatalogClosingError("closing page must contain meaningful content")
    if len(choice.heading or "") + len(choice.note or "") + sum(len(item.value) for item in contacts) > 900:
        raise CatalogClosingError("closing page content exceeds one-page print budget")
    return ResolvedCatalogClosing(
        schema_version=CLOSING_SCHEMA_VERSION, enabled=True,
        closing_key=definition.key, closing_version=definition.version,
        heading=choice.heading, note=choice.note,
        show_publisher_logo=bool(choice.show_publisher_logo if choice.show_publisher_logo is not None else True) and branding.logo is not None,
        contacts=contacts, qr_enabled=choice.qr.enabled,
        qr_target_type=qr_type, qr_target_url=qr_url,
        qr_version=QR_RENDER_VERSION if choice.qr.enabled else None,
    )


def hash_resolved_catalog_closing(closing: ResolvedCatalogClosing) -> str:
    canonical = json.dumps(closing.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def closing_qr_data_uri(closing: ResolvedCatalogClosing) -> str | None:
    if not closing.qr_enabled:
        return None
    if not closing.qr_target_url:
        raise CatalogClosingError("frozen QR target is missing")
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(closing.qr_target_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#111111", back_color="#FFFFFF")
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
