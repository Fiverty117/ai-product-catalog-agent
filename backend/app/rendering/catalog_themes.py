"""Versioned, renderer-owned catalog visual themes and palette resolution."""

import hashlib
import json
import re
from dataclasses import dataclass

from app.domain.schemas import ResolvedCatalogBranding, ResolvedCatalogTheme

THEME_SCHEMA_VERSION = "catalog-theme-v1"
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class UnknownCatalogThemeError(ValueError):
    pass


class InvalidCatalogPaletteError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogThemeDefinition:
    key: str
    version: str
    display_name: str
    description: str
    css_class: str


_THEMES = (
    CatalogThemeDefinition("minimal", "1", "Minimal", "Clean and restrained", "theme-minimal"),
    CatalogThemeDefinition("premium", "1", "Premium", "Editorial and refined", "theme-premium"),
    CatalogThemeDefinition("organic", "1", "Organic", "Soft and natural", "theme-organic"),
    CatalogThemeDefinition("bold", "1", "Bold", "High-contrast retail", "theme-bold"),
)


def catalog_theme_definitions() -> tuple[CatalogThemeDefinition, ...]:
    return _THEMES


def resolve_catalog_theme_definition(key: str, version: str) -> CatalogThemeDefinition:
    for theme in _THEMES:
        if theme.key == key and theme.version == version:
            return theme
    raise UnknownCatalogThemeError(f"unsupported catalog theme: {key}/{version}")


def canonical_color(value: str) -> str:
    if not _HEX.fullmatch(value):
        raise InvalidCatalogPaletteError("palette color must use #RRGGBB")
    return value.upper()


def contrast_foreground(background: str) -> str:
    luminance = _relative_luminance(canonical_color(background))
    dark = "#000000"
    dark_luminance = _relative_luminance(dark)
    contrast_with_dark = (luminance + 0.05) / (dark_luminance + 0.05) if luminance >= dark_luminance else (dark_luminance + 0.05) / (luminance + 0.05)
    contrast_with_white = 1.05 / (luminance + 0.05)
    return dark if contrast_with_dark >= contrast_with_white else "#FFFFFF"


def _relative_luminance(color: str) -> float:
    components = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4 for part in components]
    return sum(weight * component for weight, component in zip((0.2126, 0.7152, 0.0722), linear))


def resolve_catalog_theme(
    key: str,
    version: str,
    branding: ResolvedCatalogBranding,
    *,
    primary_color_override: str | None = None,
    accent_color_override: str | None = None,
) -> ResolvedCatalogTheme:
    definition = resolve_catalog_theme_definition(key, version)
    primary = canonical_color(primary_color_override if primary_color_override is not None else branding.primary_color)
    accent = canonical_color(accent_color_override if accent_color_override is not None else branding.accent_color)
    return ResolvedCatalogTheme(
        schema_version=THEME_SCHEMA_VERSION,
        theme_key=definition.key, theme_version=definition.version,
        css_class=definition.css_class,
        primary_color=primary, accent_color=accent,
        primary_contrast_color=contrast_foreground(primary),
        accent_contrast_color=contrast_foreground(accent),
        palette_source="custom" if primary_color_override is not None or accent_color_override is not None else "publisher",
    )


def hash_resolved_catalog_theme(theme: ResolvedCatalogTheme) -> str:
    visual = theme.model_dump(mode="json", exclude={"palette_source"})
    canonical = json.dumps(visual, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
