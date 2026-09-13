import base64
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

from app.ai.vision import (
    PermanentVisionProviderError,
    RetryableVisionProviderError,
    VisionExtractionRequest,
    VisionExtractionResponse,
)
from app.domain.schemas import ProductExtractionResult

OPENAI_PROVIDER = "openai"
DEFAULT_OPENAI_VISION_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "low"
SUPPORTED_IMAGE_DETAILS = {"auto", "low", "high", "original"}
SUPPORTED_PARAMETER_KEYS = {
    "image_detail",
    "max_output_tokens",
    "reasoning_effort",
}


def configured_openai_vision_model() -> str:
    return (
        os.environ.get("OPENAI_VISION_MODEL", DEFAULT_OPENAI_VISION_MODEL).strip()
        or DEFAULT_OPENAI_VISION_MODEL
    )


class OpenAIVisionProvider:
    """OpenAI Responses API adapter for the product extraction contract."""

    name = OPENAI_PROVIDER

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_environment(cls) -> "OpenAIVisionProvider":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise PermanentVisionProviderError(
                "OpenAI API credentials are not configured"
            )
        return cls(OpenAI(api_key=api_key, max_retries=0))

    def extract(
        self,
        request: VisionExtractionRequest,
    ) -> VisionExtractionResponse:
        parameters = _validate_parameters(request.parameters)
        if not request.images:
            raise PermanentVisionProviderError("at least one image is required")
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": "Extract product metadata from all supplied photos.",
            }
        ]
        detail = parameters["image_detail"]
        for image in request.images:
            if not image.content:
                raise PermanentVisionProviderError("image input is empty")
            if image.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise PermanentVisionProviderError("image MIME type is unsupported")
            encoded = base64.b64encode(image.content).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{image.mime_type};base64,{encoded}",
                    "detail": detail,
                }
            )

        request_options: dict[str, Any] = {
            "model": request.model,
            "instructions": request.prompt,
            "input": [{"role": "user", "content": content}],
            "text_format": ProductExtractionResult,
            "reasoning": {"effort": parameters["reasoning_effort"]},
            "store": False,
        }
        if parameters["max_output_tokens"] is not None:
            request_options["max_output_tokens"] = parameters["max_output_tokens"]

        try:
            response = self._client.responses.parse(**request_options)
        except Exception as error:
            raise _classify_openai_error(error) from None

        if _response_contains_refusal(response):
            raise PermanentVisionProviderError("OpenAI refused the extraction request")
        response_status = getattr(response, "status", None)
        if response_status == "incomplete":
            raise RetryableVisionProviderError("OpenAI response was incomplete")
        if response_status not in {None, "completed"}:
            raise RetryableVisionProviderError("OpenAI response did not complete")

        output_parsed = getattr(response, "output_parsed", None)
        if output_parsed is None:
            raise RetryableVisionProviderError(
                "OpenAI response did not contain structured output"
            )
        try:
            parsed = ProductExtractionResult.model_validate(output_parsed)
        except ValidationError:
            raise RetryableVisionProviderError(
                "OpenAI returned structured output that failed validation"
            ) from None

        return VisionExtractionResponse(
            structured_result=parsed,
            usage=_normalize_usage(response),
        )


def _validate_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(parameters) - SUPPORTED_PARAMETER_KEYS
    if unknown:
        raise PermanentVisionProviderError(
            "unsupported OpenAI extraction parameter: " + sorted(unknown)[0]
        )

    reasoning_effort = parameters.get("reasoning_effort", DEFAULT_REASONING_EFFORT)
    if reasoning_effort != DEFAULT_REASONING_EFFORT:
        raise PermanentVisionProviderError(
            f"reasoning_effort must be {DEFAULT_REASONING_EFFORT} for product.extract.v1"
        )
    image_detail = parameters.get("image_detail", "high")
    if image_detail not in SUPPORTED_IMAGE_DETAILS:
        raise PermanentVisionProviderError("image_detail is invalid")
    max_output_tokens = parameters.get("max_output_tokens")
    if max_output_tokens is not None and (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens < 1
    ):
        raise PermanentVisionProviderError("max_output_tokens must be a positive integer")
    return {
        "reasoning_effort": reasoning_effort,
        "image_detail": image_detail,
        "max_output_tokens": max_output_tokens,
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
        return RetryableVisionProviderError("temporary OpenAI service failure")
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
        return PermanentVisionProviderError("OpenAI request or access was rejected")
    if isinstance(error, APIStatusError):
        if error.status_code in {408, 409, 429} or error.status_code >= 500:
            return RetryableVisionProviderError("temporary OpenAI service failure")
        return PermanentVisionProviderError("OpenAI request or access was rejected")
    return PermanentVisionProviderError("unexpected OpenAI provider failure")


def _normalize_usage(response: Any) -> dict[str, int | str]:
    usage = getattr(response, "usage", None)
    normalized: dict[str, int | str] = {}
    response_id = getattr(response, "id", None)
    if response_id:
        normalized["provider_response_id"] = response_id
    for source_name, target_name in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = _get_value(usage, source_name)
        if value is not None:
            normalized[target_name] = value
    cached_tokens = _get_value(_get_value(usage, "input_tokens_details"), "cached_tokens")
    if cached_tokens is not None:
        normalized["cached_input_tokens"] = cached_tokens
    reasoning_tokens = _get_value(
        _get_value(usage, "output_tokens_details"), "reasoning_tokens"
    )
    if reasoning_tokens is not None:
        normalized["reasoning_tokens"] = reasoning_tokens
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
