from types import SimpleNamespace

import httpx2
import pytest
from openai import AuthenticationError, RateLimitError

from app.ai.openai_vision import OpenAIVisionProvider
from app.ai.prompts.product_extraction_v1 import PRODUCT_EXTRACTION_PROMPT, PROMPT_VERSION
from app.ai.vision import (
    PermanentVisionProviderError,
    RetryableVisionProviderError,
    VisionExtractionRequest,
    VisionImage,
)
from app.domain.schemas import ProductExtractionResult


def extraction_result() -> ProductExtractionResult:
    return ProductExtractionResult.model_validate(
        {
            "brand_name": observation("Test Brand"),
            "product_name": observation("Test Product"),
            "flavor": observation(None, state="not_present"),
            "size_value": observation("500.000000"),
            "size_unit": observation("g"),
            "servings": observation(None, state="not_legible"),
        }
    )


def observation(value, *, state: str = "extracted") -> dict:
    return {
        "value": value,
        "confidence": "0.9" if value is not None else None,
        "evidence": None,
        "state": state,
    }


def vision_request(**parameter_overrides) -> VisionExtractionRequest:
    return VisionExtractionRequest(
        images=(
            VisionImage(
                content=b"image-content",
                mime_type="image/png",
                checksum_sha256="a" * 64,
            ),
        ),
        model="gpt-5.6-sol",
        prompt=PRODUCT_EXTRACTION_PROMPT,
        parameters={
            "reasoning_effort": "low",
            "image_detail": "high",
            **parameter_overrides,
        },
    )


class FakeResponses:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def fake_client(*, response=None, error: Exception | None = None):
    responses = FakeResponses(response, error)
    return SimpleNamespace(responses=responses), responses


def test_product_extraction_prompt_is_versioned_and_observation_only() -> None:
    assert PROMPT_VERSION == "product-extraction-v1"
    assert "never infer" in PRODUCT_EXTRACTION_PROMPT.casefold()
    assert "not_legible" in PRODUCT_EXTRACTION_PROMPT
    assert "not_present" in PRODUCT_EXTRACTION_PROMPT
    assert "not invent" in PRODUCT_EXTRACTION_PROMPT.casefold()
    assert "brand_name" not in PRODUCT_EXTRACTION_PROMPT


def test_openai_responses_parse_uses_images_schema_and_low_reasoning() -> None:
    response = SimpleNamespace(
        id="resp_test_123",
        output_parsed=extraction_result(),
        usage=SimpleNamespace(
            input_tokens=120,
            input_tokens_details=SimpleNamespace(cached_tokens=20),
            output_tokens=45,
            output_tokens_details=SimpleNamespace(reasoning_tokens=15),
            total_tokens=165,
        ),
    )
    client, responses = fake_client(response=response)

    result = OpenAIVisionProvider(client).extract(vision_request())

    assert result.structured_result.product_name.value == "Test Product"
    assert result.usage == {
        "provider_response_id": "resp_test_123",
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
        "cached_input_tokens": 20,
        "reasoning_tokens": 15,
    }
    request = responses.calls[0]
    assert request["model"] == "gpt-5.6-sol"
    assert request["instructions"] == PRODUCT_EXTRACTION_PROMPT
    assert request["text_format"] is ProductExtractionResult
    assert request["reasoning"] == {"effort": "low"}
    assert request["store"] is False
    assert "tools" not in request
    content = request["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert content[1]["detail"] == "high"


def test_openai_structured_validation_failure_is_retryable() -> None:
    client, _ = fake_client(
        response=SimpleNamespace(id="resp_invalid", output_parsed={"unexpected": True})
    )

    with pytest.raises(RetryableVisionProviderError, match="failed validation"):
        OpenAIVisionProvider(client).extract(vision_request())


def test_openai_incomplete_response_is_retryable_without_parsed_output() -> None:
    client, _ = fake_client(
        response=SimpleNamespace(id="resp_incomplete", status="incomplete", output=[])
    )

    with pytest.raises(RetryableVisionProviderError) as caught:
        OpenAIVisionProvider(client).extract(vision_request())

    assert str(caught.value) == "OpenAI response was incomplete"


def test_openai_refusal_is_permanent_and_does_not_expose_refusal_text() -> None:
    client, _ = fake_client(
        response=SimpleNamespace(
            id="resp_refused",
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(type="refusal", refusal="sensitive detail")
                    ],
                )
            ],
        )
    )

    with pytest.raises(PermanentVisionProviderError) as caught:
        OpenAIVisionProvider(client).extract(vision_request())

    assert str(caught.value) == "OpenAI refused the extraction request"
    assert "sensitive detail" not in str(caught.value)


def test_openai_missing_parsed_output_is_retryable() -> None:
    client, _ = fake_client(
        response=SimpleNamespace(id="resp_empty", status="completed", output=[])
    )

    with pytest.raises(RetryableVisionProviderError) as caught:
        OpenAIVisionProvider(client).extract(vision_request())

    assert str(caught.value) == "OpenAI response did not contain structured output"


def test_openai_rate_limit_is_retryable_without_response_body() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx2.Response(429, request=request)
    client, _ = fake_client(
        error=RateLimitError("provider detail", response=response, body=None)
    )

    with pytest.raises(RetryableVisionProviderError) as caught:
        OpenAIVisionProvider(client).extract(vision_request())

    assert str(caught.value) == "temporary OpenAI service failure"


def test_openai_authentication_failure_is_permanent_and_sanitized() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx2.Response(401, request=request)
    client, _ = fake_client(
        error=AuthenticationError("provider detail", response=response, body=None)
    )

    with pytest.raises(PermanentVisionProviderError) as caught:
        OpenAIVisionProvider(client).extract(vision_request())

    assert str(caught.value) == "OpenAI request or access was rejected"


def test_openai_rejects_unsupported_parameters_before_call() -> None:
    client, responses = fake_client()

    with pytest.raises(PermanentVisionProviderError, match="unsupported"):
        OpenAIVisionProvider(client).extract(vision_request(temperature=0))

    assert responses.calls == []
