from types import SimpleNamespace

import httpx2
import pytest
from openai import AuthenticationError, BadRequestError, RateLimitError

from app.ai.openai_product_copy import OpenAIProductCopyProvider
from app.ai.product_copy import (
    PermanentProductCopyProviderError,
    ProductCopyRequest,
    RetryableProductCopyProviderError,
    configured_openai_product_copy_model,
)
from app.ai.prompts.product_copy_v1 import PRODUCT_COPY_PROMPT, PROMPT_VERSION
from app.domain.schemas import ProductCopyInputSnapshot, ProductCopyResult


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def client(response=None, error=None):
    responses = FakeResponses(response, error)
    return SimpleNamespace(responses=responses), responses


def request(**parameters):
    snapshot = ProductCopyInputSnapshot.model_validate(
        {
            "product_id": "1" * 32,
            "brand_name": "LANDERFIT",
            "product_name": "Premium Whey",
            "primary_category": {
                "category_id": "2" * 32,
                "name": "Proteinas",
            },
            "secondary_categories": [],
            "variants": [
                {
                    "sku_id": "3" * 32,
                    "flavor": "Vanilla",
                    "size_value": "2",
                    "size_unit": "LB",
                }
            ],
        }
    )
    return ProductCopyRequest(
        model="gpt-5.6-sol",
        prompt=PRODUCT_COPY_PROMPT,
        input_snapshot=snapshot,
        parameters={"reasoning_effort": "low", **parameters},
    )


def test_prompt_and_openai_request_are_grounded_structured_and_private() -> None:
    response = SimpleNamespace(
        id="resp_copy",
        status="completed",
        output_parsed={
            "short_description": "  Premium Whey LANDERFIT sabor Vanilla de 2 LB. "
        },
        output=[],
        usage=SimpleNamespace(
            input_tokens=30,
            output_tokens=12,
            total_tokens=42,
            input_tokens_details=SimpleNamespace(cached_tokens=4),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
    )
    fake_client, responses = client(response)

    returned = OpenAIProductCopyProvider(fake_client).generate(request())

    call = responses.calls[0]
    assert PROMPT_VERSION == "product-copy-v1"
    prompt = PRODUCT_COPY_PROMPT.casefold()
    for required in (
        "use only supplied facts",
        "medical",
        "nutritional quantities",
        "ingredients",
        "certifications",
        "origin",
        "instructions",
        "do not mention prices",
    ):
        assert required in prompt
    assert call["model"] == "gpt-5.6-sol"
    assert call["text_format"] is ProductCopyResult
    assert call["reasoning"] == {"effort": "low"}
    assert call["store"] is False
    assert "tools" not in call
    serialized_input = str(call["input"]).casefold()
    assert "vanilla" in serialized_input
    assert "price" not in serialized_input
    assert "image" not in serialized_input
    assert returned.structured_result.short_description == (
        "Premium Whey LANDERFIT sabor Vanilla de 2 LB."
    )
    assert returned.usage == {
        "provider_response_id": "resp_copy",
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
        "cached_input_tokens": 4,
        "reasoning_tokens": 3,
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (SimpleNamespace(status="incomplete", output=[]), "incomplete"),
        (SimpleNamespace(status="completed", output=[]), "no structured output"),
        (SimpleNamespace(status="failed", output=[]), "did not complete"),
    ],
)
def test_nonterminal_or_missing_output_is_retryable(response, message) -> None:
    fake_client, _ = client(response)
    with pytest.raises(RetryableProductCopyProviderError, match=message):
        OpenAIProductCopyProvider(fake_client).generate(request())


def test_refusal_is_permanent_and_does_not_expose_raw_refusal() -> None:
    response = SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="refusal", refusal="raw secret detail")],
            )
        ],
    )
    fake_client, _ = client(response)
    with pytest.raises(PermanentProductCopyProviderError) as error:
        OpenAIProductCopyProvider(fake_client).generate(request())
    assert "raw secret detail" not in str(error.value)


def test_malformed_structured_output_is_rejected() -> None:
    fake_client, _ = client(
        SimpleNamespace(
            status="completed",
            output=[],
            output_parsed={"short_description": "x" * 181},
        )
    )
    with pytest.raises(RetryableProductCopyProviderError, match="validation"):
        OpenAIProductCopyProvider(fake_client).generate(request())


def test_retryable_and_permanent_sdk_failures_are_classified() -> None:
    http_request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    rate_response = httpx2.Response(429, request=http_request)
    fake_client, _ = client(
        error=RateLimitError("raw body", response=rate_response, body=None)
    )
    with pytest.raises(RetryableProductCopyProviderError):
        OpenAIProductCopyProvider(fake_client).generate(request())

    auth_response = httpx2.Response(401, request=http_request)
    fake_client, _ = client(
        error=AuthenticationError("raw body", response=auth_response, body=None)
    )
    with pytest.raises(PermanentProductCopyProviderError):
        OpenAIProductCopyProvider(fake_client).generate(request())

    bad_response = httpx2.Response(400, request=http_request)
    fake_client, _ = client(
        error=BadRequestError("raw body", response=bad_response, body=None)
    )
    with pytest.raises(PermanentProductCopyProviderError):
        OpenAIProductCopyProvider(fake_client).generate(request())


def test_configuration_validation_and_environment(monkeypatch) -> None:
    fake_client, responses = client()
    with pytest.raises(PermanentProductCopyProviderError, match="unsupported"):
        OpenAIProductCopyProvider(fake_client).generate(request(temperature=0))
    assert responses.calls == []

    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(responses=FakeResponses())

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_PRODUCT_COPY_MODEL", "configured-model")
    monkeypatch.setattr("app.ai.openai_product_copy.OpenAI", fake_openai)
    OpenAIProductCopyProvider.from_environment()
    assert configured_openai_product_copy_model() == "configured-model"
    assert captured == {"api_key": "test-key", "max_retries": 0}
