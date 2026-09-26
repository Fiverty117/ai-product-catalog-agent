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
from app.ai.prompts.product_copy_v1 import PRODUCT_COPY_PROMPT as PRODUCT_COPY_PROMPT_V1
from app.ai.prompts.product_copy_v2 import PRODUCT_COPY_PROMPT, PROMPT_VERSION
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


def request(*, flavors=("Chocolate",), prompt_version=PROMPT_VERSION, **parameters):
    snapshot = ProductCopyInputSnapshot.model_validate(
        {
            "product_id": "1" * 32,
            "brand_name": "LANDERFIT",
            "product_name": "VEGAN PROTEIN",
            "primary_category": {
                "category_id": "2" * 32,
                "name": "Proteínas",
            },
            "secondary_categories": [],
            "variants": [
                {
                    "sku_id": f"{index + 3:032x}",
                    "flavor": flavor,
                    "size_value": "2.04",
                    "size_unit": "LB",
                    "servings": 25,
                }
                for index, flavor in enumerate(flavors)
            ],
        }
    )
    return ProductCopyRequest(
        model="gpt-5.6-sol",
        prompt=PRODUCT_COPY_PROMPT if prompt_version == PROMPT_VERSION else PRODUCT_COPY_PROMPT_V1,
        prompt_version=prompt_version,
        input_snapshot=snapshot,
        parameters={"reasoning_effort": "low", **parameters},
    )


def test_prompt_and_openai_request_are_grounded_structured_and_private() -> None:
    response = SimpleNamespace(
        id="resp_copy",
        status="completed",
        output_parsed={
            "short_description": "  Descripción breve del producto. "
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
    assert PROMPT_VERSION == "product-copy-v2"
    prompt = PRODUCT_COPY_PROMPT.casefold()
    for required in (
        "canonical product",
        "evergreen",
        "medical",
        "strength",
        "performance",
        "recovery",
        "muscle gain",
        "ingredients",
        "certifications",
        "origin",
        "instructions",
        "price",
        "flavor",
        "servings",
        "external sku",
    ):
        assert required in prompt
    assert call["model"] == "gpt-5.6-sol"
    assert call["text_format"] is ProductCopyResult
    assert call["reasoning"] == {"effort": "low"}
    assert call["store"] is False
    assert "tools" not in call
    serialized_input = str(call["input"]).casefold()
    assert "vegan protein" in serialized_input
    assert "landerfit" in serialized_input
    assert "proteínas" in serialized_input
    assert "chocolate" not in serialized_input
    assert "2.04" not in serialized_input
    assert "25" not in serialized_input
    assert "sku_id" not in serialized_input
    assert "price" not in serialized_input
    assert "image" not in serialized_input
    assert returned.structured_result.short_description == "Descripción breve del producto."
    assert returned.usage == {
        "provider_response_id": "resp_copy",
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
        "cached_input_tokens": 4,
        "reasoning_tokens": 3,
    }


def test_multiple_skus_do_not_enter_product_level_generation_context() -> None:
    fake_client, responses = client(
        SimpleNamespace(status="completed", output=[], output_parsed={"short_description": "Descripción general."})
    )
    OpenAIProductCopyProvider(fake_client).generate(
        request(flavors=("Chocolate", "Vanilla", "Strawberry"))
    )
    serialized_input = str(responses.calls[0]["input"]).casefold()
    assert all(flavor not in serialized_input for flavor in ("chocolate", "vanilla", "strawberry"))
    assert "vegan protein" in serialized_input


def test_v1_queued_request_keeps_its_original_context() -> None:
    fake_client, responses = client(
        SimpleNamespace(status="completed", output=[], output_parsed={"short_description": "Descripción anterior."})
    )
    OpenAIProductCopyProvider(fake_client).generate(request(prompt_version="product-copy-v1"))
    assert "chocolate" in str(responses.calls[0]["input"]).casefold()


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
