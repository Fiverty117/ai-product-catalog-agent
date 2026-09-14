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

from app.ai.category_suggestion import (
    OPENAI_CATEGORY_PROVIDER,
    CategorySuggestionRequest,
    CategorySuggestionResponse,
    PermanentCategorySuggestionProviderError,
    RetryableCategorySuggestionProviderError,
    normalize_category_parameters,
)
from app.domain.schemas import CategorySuggestionResult


class OpenAICategorySuggestionProvider:
    """OpenAI Responses API adapter for category suggestions."""

    name = OPENAI_CATEGORY_PROVIDER

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_environment(cls) -> "OpenAICategorySuggestionProvider":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise PermanentCategorySuggestionProviderError(
                "OpenAI API credentials are not configured"
            )
        return cls(OpenAI(api_key=api_key, max_retries=0))

    def suggest(
        self,
        request: CategorySuggestionRequest,
    ) -> CategorySuggestionResponse:
        parameters = normalize_category_parameters(request.parameters)
        snapshot_json = json.dumps(
            request.input_snapshot.model_dump(mode="json", exclude_none=True),
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
                            "text": "Canonical categorization input:\n" + snapshot_json,
                        }
                    ],
                }
            ],
            "text_format": CategorySuggestionResult,
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
            raise PermanentCategorySuggestionProviderError(
                "OpenAI refused the category suggestion request"
            )
        status = getattr(response, "status", None)
        if status == "incomplete":
            raise RetryableCategorySuggestionProviderError(
                "OpenAI category suggestion response was incomplete"
            )
        if status not in {None, "completed"}:
            raise RetryableCategorySuggestionProviderError(
                "OpenAI category suggestion response did not complete"
            )
        output_parsed = getattr(response, "output_parsed", None)
        if output_parsed is None:
            raise RetryableCategorySuggestionProviderError(
                "OpenAI category suggestion response had no structured output"
            )
        taxonomy_ids = {
            category.category_id for category in request.input_snapshot.taxonomy
        }
        try:
            parsed = CategorySuggestionResult.model_validate(
                output_parsed,
                context={"taxonomy_ids": taxonomy_ids},
            )
        except ValidationError:
            raise RetryableCategorySuggestionProviderError(
                "OpenAI category suggestion output failed validation"
            ) from None
        return CategorySuggestionResponse(
            structured_result=parsed,
            usage=_normalize_usage(response),
        )


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
        return RetryableCategorySuggestionProviderError(
            "temporary OpenAI category suggestion failure"
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
        return PermanentCategorySuggestionProviderError(
            "OpenAI category suggestion request or access was rejected"
        )
    if isinstance(error, APIStatusError):
        if error.status_code in {408, 409, 429} or error.status_code >= 500:
            return RetryableCategorySuggestionProviderError(
                "temporary OpenAI category suggestion failure"
            )
        return PermanentCategorySuggestionProviderError(
            "OpenAI category suggestion request or access was rejected"
        )
    return PermanentCategorySuggestionProviderError(
        "unexpected OpenAI category suggestion failure"
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
