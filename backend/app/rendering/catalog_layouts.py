from dataclasses import dataclass
from typing import Literal


CatalogLayoutKey = Literal["classic", "dense", "compact"]


class UnknownCatalogLayoutError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogLayoutDefinition:
    key: CatalogLayoutKey
    version: str
    products_per_row: int
    page_size: Literal["A4"]
    orientation: Literal["portrait"]
    css_class: str


_CATALOG_LAYOUTS: dict[CatalogLayoutKey, CatalogLayoutDefinition] = {
    "classic": CatalogLayoutDefinition(
        key="classic",
        version="1",
        products_per_row=2,
        page_size="A4",
        orientation="portrait",
        css_class="layout-classic",
    ),
    "dense": CatalogLayoutDefinition(
        key="dense",
        version="1",
        products_per_row=3,
        page_size="A4",
        orientation="portrait",
        css_class="layout-dense",
    ),
    "compact": CatalogLayoutDefinition(
        key="compact",
        version="1",
        products_per_row=4,
        page_size="A4",
        orientation="portrait",
        css_class="layout-compact",
    ),
}


def resolve_catalog_layout(key: str) -> CatalogLayoutDefinition:
    try:
        return _CATALOG_LAYOUTS[key]  # type: ignore[index]
    except KeyError:
        raise UnknownCatalogLayoutError(f"unsupported catalog layout: {key}") from None


def catalog_layout_definitions() -> tuple[CatalogLayoutDefinition, ...]:
    return tuple(_CATALOG_LAYOUTS[key] for key in ("classic", "dense", "compact"))
