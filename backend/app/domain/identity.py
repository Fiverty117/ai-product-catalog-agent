import unicodedata


class InvalidIdentityNameError(ValueError):
    pass


def clean_identity_display_name(name: str) -> str:
    """Apply harmless whitespace cleanup without rewriting human capitalization."""

    if not isinstance(name, str):
        raise InvalidIdentityNameError("identity name must be a string")
    cleaned = " ".join(name.split())
    if not cleaned:
        raise InvalidIdentityNameError("identity name must not be empty")
    return cleaned


def identity_key_v1(name: str) -> str:
    """Return the stable exact-match key defined by Identity Key v1."""

    normalized = unicodedata.normalize("NFKC", name)
    cleaned = clean_identity_display_name(normalized)
    return cleaned.casefold()


def loose_candidate_key(name: str) -> str:
    """Return an advisory key that ignores whitespace and Unicode punctuation."""

    exact_key = identity_key_v1(name)
    loose_key = "".join(
        character
        for character in exact_key
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )
    return loose_key or exact_key
