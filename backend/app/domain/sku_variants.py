import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from app.db.models import SKU


VariantTuple = tuple[str | None, Decimal | None, str | None, int | None]


def normalize_variant_text(value: str | None) -> str | None:
    """Normalize text for comparison without changing its stored display form."""

    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def variant_tuple(
    *,
    flavor: str | None,
    size_value: Decimal | None,
    size_unit: str | None,
    servings: int | None,
) -> VariantTuple:
    return (
        normalize_variant_text(flavor),
        size_value,
        normalize_variant_text(size_unit),
        servings,
    )


def sku_variant_tuple(sku: SKU) -> VariantTuple:
    return variant_tuple(
        flavor=sku.flavor,
        size_value=sku.size_value,
        size_unit=sku.size_unit,
        servings=sku.servings,
    )


def is_partial_variant_match(left: VariantTuple, right: VariantTuple) -> bool:
    """Return a suggestion only when known shared dimensions do not conflict."""

    comparable = [
        (left_value, right_value)
        for left_value, right_value in zip(left, right, strict=True)
        if left_value is not None and right_value is not None
    ]
    return bool(comparable) and all(
        left_value == right_value for left_value, right_value in comparable
    )


@dataclass(frozen=True)
class SKUVariantCandidates:
    exact: tuple[SKU, ...]
    partial: tuple[SKU, ...]
    external_sku: tuple[SKU, ...]
