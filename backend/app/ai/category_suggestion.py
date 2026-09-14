import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.domain.schemas import CategorySuggestionInputSnapshot, CategorySuggestionResult
from app.services.jobs import PermanentJobError

OPENAI_CATEGORY_PROVIDER = "openai"
DEFAULT_OPENAI_CATEGORY_MODEL = "gpt-5.6-sol"
DEFAULT_CATEGORY_REASONING_EFFORT = "low"
CATEGORY_PARAMETER_KEYS = {"max_output_tokens", "reasoning_effort"}


def configured_openai_category_model() -> str:
    return (
        os.environ.get("OPENAI_CATEGORY_MODEL", DEFAULT_OPENAI_CATEGORY_MODEL).strip()
        or DEFAULT_OPENAI_CATEGORY_MODEL
    )


def normalize_category_parameters(
    parameters: Mapping[str, object] | None,
) -> dict[str, object]:
    supplied = dict(parameters or {})
    unknown = set(supplied) - CATEGORY_PARAMETER_KEYS
    if unknown:
        raise PermanentCategorySuggestionProviderError(
            "unsupported OpenAI category parameter: " + sorted(unknown)[0]
        )
    reasoning_effort = supplied.get(
        "reasoning_effort", DEFAULT_CATEGORY_REASONING_EFFORT
    )
    if reasoning_effort != DEFAULT_CATEGORY_REASONING_EFFORT:
        raise PermanentCategorySuggestionProviderError(
            "reasoning_effort must be low for product.category_suggest.v1"
        )
    max_output_tokens = supplied.get("max_output_tokens")
    if max_output_tokens is not None and (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens < 1
    ):
        raise PermanentCategorySuggestionProviderError(
            "max_output_tokens must be a positive integer"
        )
    return {
        "reasoning_effort": reasoning_effort,
        **(
            {"max_output_tokens": max_output_tokens}
            if max_output_tokens is not None
            else {}
        ),
    }


@dataclass(frozen=True)
class CategorySuggestionRequest:
    model: str
    prompt: str
    input_snapshot: CategorySuggestionInputSnapshot
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class CategorySuggestionResponse:
    structured_result: CategorySuggestionResult
    usage: dict[str, int | str | None]


class CategorySuggestionProvider(Protocol):
    name: str

    def suggest(
        self, request: CategorySuggestionRequest
    ) -> CategorySuggestionResponse: ...


class CategorySuggestionProviderError(RuntimeError):
    pass


class RetryableCategorySuggestionProviderError(CategorySuggestionProviderError):
    pass


class PermanentCategorySuggestionProviderError(
    CategorySuggestionProviderError, PermanentJobError
):
    pass
