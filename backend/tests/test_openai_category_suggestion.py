from types import SimpleNamespace

import httpx2
import pytest
from openai import AuthenticationError, BadRequestError, RateLimitError

from app.ai.category_suggestion import (
    CategorySuggestionRequest,
    PermanentCategorySuggestionProviderError,
    RetryableCategorySuggestionProviderError,
    configured_openai_category_model,
)
from app.ai.openai_category_suggestion import OpenAICategorySuggestionProvider
from app.ai.prompts.product_category_v1 import PRODUCT_CATEGORY_PROMPT, PROMPT_VERSION
from app.domain.schemas import CategorySuggestionInputSnapshot, CategorySuggestionResult


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


def request(category_id, **parameters):
    snapshot = CategorySuggestionInputSnapshot.model_validate(
        {
            "product": {"product_id": "1" * 32, "product_name": "Whey"},
            "brand": {"brand_id": "2" * 32, "brand_name": "Brand"},
            "sku_variants": [],
            "taxonomy": [
                {
                    "category_id": category_id,
                    "name": "Proteins",
                    "identity_key": "proteins",
                    "sort_order": 10,
                }
            ],
        }
    )
    return CategorySuggestionRequest(
        model="gpt-5.6-sol",
        prompt=PRODUCT_CATEGORY_PROMPT,
        input_snapshot=snapshot,
        parameters={"reasoning_effort": "low", **parameters},
    )


def result(category_id):
    return CategorySuggestionResult.model_validate(
        {
            "primary": {
                "category_id": category_id,
                "confidence": "0.9",
                "evidence": "Product name identifies whey protein.",
            },
            "secondary": [],
        }
    )


def test_prompt_and_openai_request_are_narrow_and_private() -> None:
    category_id = "3" * 32
    response = SimpleNamespace(
        id="resp_category",
        status="completed",
        output_parsed=result(category_id),
        output=[],
        usage=SimpleNamespace(
            input_tokens=50,
            output_tokens=20,
            total_tokens=70,
            input_tokens_details=SimpleNamespace(cached_tokens=5),
            output_tokens_details=SimpleNamespace(reasoning_tokens=4),
        ),
    )
    fake_client, responses = client(response)

    returned = OpenAICategorySuggestionProvider(fake_client).suggest(
        request(category_id)
    )

    call = responses.calls[0]
    assert PROMPT_VERSION == "product-category-v1"
    assert "not separate" in PRODUCT_CATEGORY_PROMPT
    assert "never invent" in PRODUCT_CATEGORY_PROMPT.casefold()
    assert call["model"] == "gpt-5.6-sol"
    assert call["text_format"] is CategorySuggestionResult
    assert call["reasoning"] == {"effort": "low"}
    assert call["store"] is False
    assert "tools" not in call
    assert "image" not in str(call["input"]).casefold()
    assert returned.usage == {
        "provider_response_id": "resp_category",
        "input_tokens": 50,
        "output_tokens": 20,
        "total_tokens": 70,
        "cached_input_tokens": 5,
        "reasoning_tokens": 4,
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (SimpleNamespace(status="incomplete", output=[]), "incomplete"),
        (SimpleNamespace(status="completed", output=[]), "no structured output"),
    ],
)
def test_incomplete_or_missing_output_is_retryable(response, message) -> None:
    fake_client, _ = client(response)
    with pytest.raises(RetryableCategorySuggestionProviderError, match=message):
        OpenAICategorySuggestionProvider(fake_client).suggest(request("3" * 32))


def test_refusal_is_permanent_and_sanitized() -> None:
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
    with pytest.raises(PermanentCategorySuggestionProviderError) as error:
        OpenAICategorySuggestionProvider(fake_client).suggest(request("3" * 32))
    assert "raw secret detail" not in str(error.value)


def test_unknown_taxonomy_id_from_provider_is_retryable() -> None:
    fake_client, _ = client(
        SimpleNamespace(
            status="completed",
            output=[],
            output_parsed=result("4" * 32),
        )
    )
    with pytest.raises(RetryableCategorySuggestionProviderError, match="validation"):
        OpenAICategorySuggestionProvider(fake_client).suggest(request("3" * 32))


def test_retryable_and_permanent_sdk_failures_are_classified() -> None:
    http_request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    rate_response = httpx2.Response(429, request=http_request)
    fake_client, _ = client(
        error=RateLimitError("raw body", response=rate_response, body=None)
    )
    with pytest.raises(RetryableCategorySuggestionProviderError):
        OpenAICategorySuggestionProvider(fake_client).suggest(request("3" * 32))

    auth_response = httpx2.Response(401, request=http_request)
    fake_client, _ = client(
        error=AuthenticationError("raw body", response=auth_response, body=None)
    )
    with pytest.raises(PermanentCategorySuggestionProviderError):
        OpenAICategorySuggestionProvider(fake_client).suggest(request("3" * 32))

    bad_response = httpx2.Response(400, request=http_request)
    fake_client, _ = client(
        error=BadRequestError("raw body", response=bad_response, body=None)
    )
    with pytest.raises(PermanentCategorySuggestionProviderError):
        OpenAICategorySuggestionProvider(fake_client).suggest(request("3" * 32))


def test_unsupported_configuration_is_rejected_before_provider_call() -> None:
    fake_client, responses = client()
    with pytest.raises(PermanentCategorySuggestionProviderError, match="unsupported"):
        OpenAICategorySuggestionProvider(fake_client).suggest(
            request("3" * 32, temperature=0)
        )
    assert responses.calls == []


def test_environment_configuration_uses_category_model_and_disables_sdk_retries(
    monkeypatch,
) -> None:
    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(responses=FakeResponses())

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_CATEGORY_MODEL", "configured-model")
    monkeypatch.setattr(
        "app.ai.openai_category_suggestion.OpenAI",
        fake_openai,
    )

    OpenAICategorySuggestionProvider.from_environment()

    assert configured_openai_category_model() == "configured-model"
    assert captured == {"api_key": "test-key", "max_retries": 0}
