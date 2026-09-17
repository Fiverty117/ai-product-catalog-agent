import re
import unicodedata


MAX_PRODUCT_SHORT_DESCRIPTION_LENGTH = 180


def normalize_product_short_description(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("short_description must be text")
    raw_lines = value.splitlines() or [value]
    if any(re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line) for line in raw_lines):
        raise ValueError("short_description must not contain a list")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("short_description must not be empty")
    if len(normalized) > MAX_PRODUCT_SHORT_DESCRIPTION_LENGTH:
        raise ValueError("short_description must be at most 180 characters")
    if (
        re.search(r"<\s*/?\s*[A-Za-z][^>]*>", normalized)
        or re.search(r"!?\[[^\]]+\]\([^\)]+\)", normalized)
        or "`" in normalized
        or "**" in normalized
        or "__" in normalized
    ):
        raise ValueError("short_description must be plain text")
    return normalized
