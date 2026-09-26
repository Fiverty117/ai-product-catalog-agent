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
from app.ai.prompts.product_copy_v1 import (
    PRODUCT_COPY_PROMPT as PRODUCT_COPY_PROMPT_V1,
    PROMPT_VERSION as PROMPT_VERSION_V1,
)
from app.ai.prompts.product_copy_v2 import (
    PRODUCT_COPY_PROMPT as PRODUCT_COPY_PROMPT_V2,
    PROMPT_VERSION as PROMPT_VERSION_V2,
)
from app.ai.prompts.product_copy_v3 import PRODUCT_COPY_PROMPT, PROMPT_VERSION
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


PROMPTS = {
    PROMPT_VERSION_V1: PRODUCT_COPY_PROMPT_V1,
    PROMPT_VERSION_V2: PRODUCT_COPY_PROMPT_V2,
    PROMPT_VERSION: PRODUCT_COPY_PROMPT,
}


def request(
    *,
    product_name="VEGAN PROTEIN",
    brand_name="LANDERFIT",
    category_name="Proteínas",
    flavors=("Chocolate",),
    prompt_version=PROMPT_VERSION,
    **parameters,
):
    snapshot = ProductCopyInputSnapshot.model_validate(
        {
            "product_id": "1" * 32,
            "brand_name": brand_name,
            "product_name": product_name,
            "primary_category": {
                "category_id": "2" * 32,
                "name": category_name,
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
        prompt=PROMPTS[prompt_version],
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
    assert PROMPT_VERSION == "product-copy-v3"
    prompt = " ".join(PRODUCT_COPY_PROMPT.casefold().split())
    for required in (
        "canonical product",
        "established general knowledge",
        "common uses",
        "generally associated benefits",
        "approved claim record",
        "evergreen",
        "medical",
        "strength",
        "performance",
        "dosage",
        "concentration",
        "purity",
        "certifications",
        "origin",
        "laboratory testing",
        "two concise",
        "180 characters",
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
    assert "individual sku" in responses.calls[0]["instructions"].casefold()


def test_collagen_prompt_allows_general_benefits_without_internal_claims() -> None:
    fake_client, responses = client(
        SimpleNamespace(
            status="completed",
            output=[],
            output_parsed={"short_description": "Descripción comercial."},
        )
    )

    OpenAIProductCopyProvider(fake_client).generate(
        request(
            product_name="COLÁGENO HIDROLIZADO",
            category_name="Suplementos / Colágeno",
        )
    )

    prompt = " ".join(responses.calls[0]["instructions"].casefold().split())
    serialized_input = str(responses.calls[0]["input"]).casefold()
    assert "colágeno hidrolizado" in serialized_input
    assert "established general knowledge" in prompt
    assert "common uses" in prompt
    assert "generally associated benefits" in prompt
    assert "even when no internal product fact or approved claim record" in prompt
    for forbidden_invention in (
        "dosage",
        "concentration",
        "certifications",
        "disease-treatment",
        "disease-prevention",
    ):
        assert forbidden_invention in prompt


def test_creatine_prompt_allows_sports_context_without_specific_claims() -> None:
    fake_client, responses = client(
        SimpleNamespace(
            status="completed",
            output=[],
            output_parsed={"short_description": "Descripción comercial."},
        )
    )

    OpenAIProductCopyProvider(fake_client).generate(
        request(
            product_name="CREATINA MONOHIDRATADA",
            brand_name="ExampleBrand",
            category_name="Suplementos / Creatina",
        )
    )

    prompt = " ".join(responses.calls[0]["instructions"].casefold().split())
    serialized_input = str(responses.calls[0]["input"]).casefold()
    assert "creatina monohidratada" in serialized_input
    assert "examplebrand" in serialized_input
    assert "strength-training" in prompt
    assert "high-intensity" in prompt
    assert "sports-performance context is allowed" in prompt
    assert "do not invent medical effects" not in prompt
    assert "purity" in prompt
    assert "dosage" in prompt
    assert "certifications" in prompt
    assert "country of origin" in prompt
    assert "specific formulation" in prompt
    assert "laboratory testing" in prompt


def test_medical_boundary_preserves_general_wellness_and_sports_benefits() -> None:
    prompt = " ".join(PRODUCT_COPY_PROMPT.casefold().split())
    assert "do not make disease-treatment or disease-prevention claims" in prompt
    assert "does not prohibit normal general wellness" in prompt
    assert "nutrition or sports benefits" in prompt
    assert "normal nutritional, wellness" in prompt


def test_v1_queued_request_keeps_its_original_context() -> None:
    fake_client, responses = client(
        SimpleNamespace(status="completed", output=[], output_parsed={"short_description": "Descripción anterior."})
    )
    OpenAIProductCopyProvider(fake_client).generate(
        request(prompt_version=PROMPT_VERSION_V1)
    )
    assert "chocolate" in str(responses.calls[0]["input"]).casefold()


def test_v2_queued_request_keeps_product_level_context() -> None:
    fake_client, responses = client(
        SimpleNamespace(
            status="completed",
            output=[],
            output_parsed={"short_description": "Descripción anterior."},
        )
    )
    OpenAIProductCopyProvider(fake_client).generate(
        request(prompt_version=PROMPT_VERSION_V2)
    )
    serialized_input = str(responses.calls[0]["input"]).casefold()
    assert "vegan protein" in serialized_input
    assert "chocolate" not in serialized_input


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
