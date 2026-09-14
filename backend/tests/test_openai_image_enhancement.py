import base64
from types import SimpleNamespace

import httpx2
import pytest
from openai import AuthenticationError, RateLimitError

from app.ai.image_enhancement import (
    ImageEnhancementRequest,
    ImageEnhancementSource,
    PermanentImageEnhancementProviderError,
    RetryableImageEnhancementProviderError,
    configured_openai_image_model,
)
from app.ai.openai_image_enhancement import OpenAIImageEnhancementProvider
from app.ai.prompts.product_image_enhancement_v1 import (
    PRODUCT_IMAGE_ENHANCEMENT_PROMPT,
    PROMPT_VERSION,
)


class FakeImages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def edit(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def client(response=None, error=None):
    images = FakeImages(response, error)
    return SimpleNamespace(images=images), images


def request(**parameters):
    return ImageEnhancementRequest(
        source=ImageEnhancementSource(
            content=b"transient-source-bytes",
            mime_type="image/png",
            checksum_sha256="a" * 64,
        ),
        model="gpt-image-2.5-sunburst",
        prompt=PRODUCT_IMAGE_ENHANCEMENT_PROMPT,
        parameters={"quality": "high", **parameters},
    )


def test_prompt_is_versioned_generic_and_preservation_focused() -> None:
    prompt = PRODUCT_IMAGE_ENHANCEMENT_PROMPT.casefold()
    assert PROMPT_VERSION == "product-image-enhancement-v1"
    assert "catalog" in prompt and "studio" in prompt
    assert "preserve" in prompt
    assert "logo" in prompt and "label text" in prompt
    assert "grabelan" not in prompt


def test_openai_image_edit_uses_model_quality_and_transient_source_bytes() -> None:
    output = b"generated-image-bytes"
    response = SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(output).decode(), output_format="png")],
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=20,
            total_tokens=32,
            input_tokens_details=SimpleNamespace(image_tokens=10, text_tokens=2),
        ),
    )
    fake_client, images = client(response=response)

    result = OpenAIImageEnhancementProvider(fake_client).enhance(request())

    assert result.output_bytes == output
    assert result.output_format_hint == "png"
    assert result.usage == {
        "input_tokens": 12,
        "output_tokens": 20,
        "total_tokens": 32,
        "input_image_tokens": 10,
        "input_text_tokens": 2,
    }
    call = images.calls[0]
    assert call["model"] == "gpt-image-2.5-sunburst"
    assert call["quality"] == "high"
    assert call["output_format"] == "png"
    assert call["size"] == "auto"
    assert call["background"] == "auto"
    assert "response_format" not in call
    assert call["prompt"] == PRODUCT_IMAGE_ENHANCEMENT_PROMPT
    assert call["image"] == (
        "source.png",
        b"transient-source-bytes",
        "image/png",
    )
    assert "tools" not in call and "web_search" not in call


def test_missing_output_is_retryable_and_invalid_base64_is_permanent() -> None:
    missing_client, _ = client(response=SimpleNamespace(data=[]))
    invalid_client, _ = client(
        response=SimpleNamespace(data=[SimpleNamespace(b64_json="not base64!!")])
    )

    with pytest.raises(RetryableImageEnhancementProviderError, match="no output"):
        OpenAIImageEnhancementProvider(missing_client).enhance(request())
    with pytest.raises(PermanentImageEnhancementProviderError, match="invalid output"):
        OpenAIImageEnhancementProvider(invalid_client).enhance(request())


def test_openai_retryable_and_permanent_errors_are_sanitized() -> None:
    request_object = httpx2.Request("POST", "https://api.openai.com/v1/images/edits")
    rate_response = httpx2.Response(429, request=request_object)
    auth_response = httpx2.Response(401, request=request_object)
    retry_client, _ = client(
        error=RateLimitError("raw body", response=rate_response, body=None)
    )
    auth_client, _ = client(
        error=AuthenticationError("raw body", response=auth_response, body=None)
    )

    with pytest.raises(RetryableImageEnhancementProviderError) as retry:
        OpenAIImageEnhancementProvider(retry_client).enhance(request())
    with pytest.raises(PermanentImageEnhancementProviderError) as permanent:
        OpenAIImageEnhancementProvider(auth_client).enhance(request())

    assert str(retry.value) == "temporary OpenAI image enhancement failure"
    assert str(permanent.value) == (
        "OpenAI image enhancement request or access was rejected"
    )
    assert "raw body" not in str(retry.value) + str(permanent.value)


def test_invalid_configuration_rejected_before_call() -> None:
    fake_client, images = client()
    with pytest.raises(PermanentImageEnhancementProviderError, match="quality"):
        OpenAIImageEnhancementProvider(fake_client).enhance(request(quality="ultra"))
    assert images.calls == []


def test_environment_model_and_sdk_retry_configuration(monkeypatch) -> None:
    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(images=FakeImages())

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "configured-image-model")
    monkeypatch.setattr("app.ai.openai_image_enhancement.OpenAI", fake_openai)

    OpenAIImageEnhancementProvider.from_environment()

    assert configured_openai_image_model() == "configured-image-model"
    assert captured == {"api_key": "test-key", "max_retries": 0}
