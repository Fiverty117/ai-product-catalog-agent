import json
import os
from collections.abc import Mapping
from typing import Any

from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ContentFilterFinishReasonError,
    InternalServerError,
    LengthFinishReasonError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import ValidationError

from app.ai.product_copy import (
    OPENAI_PRODUCT_COPY_PROVIDER,
    PermanentProductCopyProviderError,
    ProductCopyRequest,
    ProductCopyResponse,
    RetryableProductCopyProviderError,
    normalize_product_copy_parameters,
)
from app.ai.prompts.product_copy_v1 import PROMPT_VERSION as PROMPT_VERSION_V1
from app.ai.prompts.product_copy_v2 import PROMPT_VERSION as PROMPT_VERSION_V2
from app.ai.prompts.product_copy_v3 import PROMPT_VERSION as PROMPT_VERSION_V3
from app.domain.schemas import ProductCopyResult


class OpenAIProductCopyProvider:
    """OpenAI Responses API adapter for structured Product copy."""

    name = OPENAI_PRODUCT_COPY_PROVIDER

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_environment(cls) -> "OpenAIProductCopyProvider":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise PermanentProductCopyProviderError(
                "OpenAI API credentials are not configured"
            )
        return cls(OpenAI(api_key=api_key, max_retries=0))

    def generate(self, request: ProductCopyRequest) -> ProductCopyResponse:
        parameters = normalize_product_copy_parameters(request.parameters)
        facts_json = json.dumps(
            _generation_facts(request),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        options: dict[str, Any] = {
            "model": request.model,
            "instructions": request.prompt,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Canonical Product facts:\n" + facts_json,
                        }
                    ],
                }
            ],
            "text_format": ProductCopyResult,
            "reasoning": {"effort": parameters["reasoning_effort"]},
            "store": False,
        }
        if "max_output_tokens" in parameters:
            options["max_output_tokens"] = parameters["max_output_tokens"]

        try:
            response = self._client.responses.parse(**options)
        except Exception as error:
            raise _classify_openai_error(error) from None

        if _response_contains_refusal(response):
            raise PermanentProductCopyProviderError(
                "OpenAI refused the Product copy request"
            )
        status = getattr(response, "status", None)
        if status == "incomplete":
            raise RetryableProductCopyProviderError(
                "OpenAI Product copy response was incomplete"
            )
        if status not in {None, "completed"}:
            raise RetryableProductCopyProviderError(
                "OpenAI Product copy response did not complete"
            )
        output_parsed = getattr(response, "output_parsed", None)
        if output_parsed is None:
            raise RetryableProductCopyProviderError(
                "OpenAI Product copy response had no structured output"
            )
        try:
            parsed = ProductCopyResult.model_validate(output_parsed)
        except ValidationError:
            raise RetryableProductCopyProviderError(
                "OpenAI Product copy output failed validation"
            ) from None
        return ProductCopyResponse(
            structured_result=parsed,
            usage=_normalize_usage(response),
        )


def _generation_facts(request: ProductCopyRequest) -> dict[str, Any]:
    if request.prompt_version == PROMPT_VERSION_V1:
        # Preserve the request semantics of jobs queued before the editorial fix.
        return request.input_snapshot.model_dump(mode="json", exclude_none=True)
    if request.prompt_version not in {PROMPT_VERSION_V2, PROMPT_VERSION_V3}:
        raise PermanentProductCopyProviderError("unsupported Product copy prompt version")
    snapshot = request.input_snapshot
    return {
        "brand_name": snapshot.brand_name,
        "product_name": snapshot.product_name,
        "primary_category": (
            snapshot.primary_category.name if snapshot.primary_category else None
        ),
        "secondary_categories": [category.name for category in snapshot.secondary_categories],
    }


def _classify_openai_error(error: Exception) -> Exception:
    if isinstance(
        error,
        (
            APITimeoutError,
            APIConnectionError,
            APIResponseValidationError,
            RateLimitError,
            InternalServerError,
            LengthFinishReasonError,
        ),
    ):
        return RetryableProductCopyProviderError(
            "temporary OpenAI Product copy failure"
        )
    if isinstance(
        error,
        (
            AuthenticationError,
            PermissionDeniedError,
            NotFoundError,
            BadRequestError,
            ContentFilterFinishReasonError,
            UnprocessableEntityError,
        ),
    ):
        return PermanentProductCopyProviderError(
            "OpenAI Product copy request or access was rejected"
        )
    if isinstance(error, APIStatusError):
        if error.status_code in {408, 409, 429} or error.status_code >= 500:
            return RetryableProductCopyProviderError(
                "temporary OpenAI Product copy failure"
            )
        return PermanentProductCopyProviderError(
            "OpenAI Product copy request or access was rejected"
        )
    return PermanentProductCopyProviderError(
        "unexpected OpenAI Product copy failure"
    )


def _normalize_usage(response: Any) -> dict[str, int | str]:
    usage = getattr(response, "usage", None)
    normalized: dict[str, int | str] = {}
    response_id = getattr(response, "id", None)
    if response_id:
        normalized["provider_response_id"] = response_id
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = _get_value(usage, name)
        if value is not None:
            normalized[name] = value
    cached = _get_value(_get_value(usage, "input_tokens_details"), "cached_tokens")
    if cached is not None:
        normalized["cached_input_tokens"] = cached
    reasoning = _get_value(
        _get_value(usage, "output_tokens_details"), "reasoning_tokens"
    )
    if reasoning is not None:
        normalized["reasoning_tokens"] = reasoning
    return normalized


def _response_contains_refusal(response: Any) -> bool:
    for output_item in getattr(response, "output", None) or ():
        if _get_value(output_item, "type") != "message":
            continue
        for content_item in _get_value(output_item, "content") or ():
            if _get_value(content_item, "type") == "refusal":
                return True
    return False


def _get_value(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)
