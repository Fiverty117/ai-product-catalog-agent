import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.domain.schemas import ProductCopyInputSnapshot, ProductCopyResult
from app.services.jobs import PermanentJobError

OPENAI_PRODUCT_COPY_PROVIDER = "openai"
DEFAULT_OPENAI_PRODUCT_COPY_MODEL = "gpt-5.6-sol"
DEFAULT_PRODUCT_COPY_REASONING_EFFORT = "low"
PRODUCT_COPY_PARAMETER_KEYS = {"max_output_tokens", "reasoning_effort"}


def configured_openai_product_copy_model() -> str:
    return (
        os.environ.get(
            "OPENAI_PRODUCT_COPY_MODEL", DEFAULT_OPENAI_PRODUCT_COPY_MODEL
        ).strip()
        or DEFAULT_OPENAI_PRODUCT_COPY_MODEL
    )


def normalize_product_copy_parameters(
    parameters: Mapping[str, object] | None,
) -> dict[str, object]:
    supplied = dict(parameters or {})
    unknown = set(supplied) - PRODUCT_COPY_PARAMETER_KEYS
    if unknown:
        raise PermanentProductCopyProviderError(
            "unsupported OpenAI Product copy parameter: " + sorted(unknown)[0]
        )
    reasoning_effort = supplied.get(
        "reasoning_effort", DEFAULT_PRODUCT_COPY_REASONING_EFFORT
    )
    if reasoning_effort != DEFAULT_PRODUCT_COPY_REASONING_EFFORT:
        raise PermanentProductCopyProviderError(
            "reasoning_effort must be low for product.copy.v1"
        )
    max_output_tokens = supplied.get("max_output_tokens")
    if max_output_tokens is not None and (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens < 1
    ):
        raise PermanentProductCopyProviderError(
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
class ProductCopyRequest:
    model: str
    prompt: str
    input_snapshot: ProductCopyInputSnapshot
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class ProductCopyResponse:
    structured_result: ProductCopyResult
    usage: dict[str, int | str | None]


class ProductCopyProvider(Protocol):
    name: str

    def generate(self, request: ProductCopyRequest) -> ProductCopyResponse: ...


class ProductCopyProviderError(RuntimeError):
    pass


class RetryableProductCopyProviderError(ProductCopyProviderError):
    pass


class PermanentProductCopyProviderError(ProductCopyProviderError, PermanentJobError):
    pass
