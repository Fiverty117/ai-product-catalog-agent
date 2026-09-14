import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.services.jobs import PermanentJobError

OPENAI_IMAGE_PROVIDER = "openai"
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2.5-sunburst"
DEFAULT_IMAGE_QUALITY = "high"
SUPPORTED_IMAGE_QUALITIES = {"low", "medium", "high", "xhigh", "max", "auto"}
SUPPORTED_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
SUPPORTED_IMAGE_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
SUPPORTED_BACKGROUNDS = {"transparent", "opaque", "auto"}
IMAGE_PARAMETER_KEYS = {"quality", "output_format", "size", "background"}


def configured_openai_image_model() -> str:
    return (
        os.environ.get("OPENAI_IMAGE_MODEL", DEFAULT_OPENAI_IMAGE_MODEL).strip()
        or DEFAULT_OPENAI_IMAGE_MODEL
    )


def normalize_image_enhancement_parameters(
    parameters: Mapping[str, object] | None,
) -> dict[str, object]:
    supplied = dict(parameters or {})
    unknown = set(supplied) - IMAGE_PARAMETER_KEYS
    if unknown:
        raise PermanentImageEnhancementProviderError(
            "unsupported image enhancement parameter: " + sorted(unknown)[0]
        )
    quality = supplied.get("quality", DEFAULT_IMAGE_QUALITY)
    output_format = supplied.get("output_format", "png")
    size = supplied.get("size", "auto")
    background = supplied.get("background", "auto")
    if quality not in SUPPORTED_IMAGE_QUALITIES:
        raise PermanentImageEnhancementProviderError("image quality is invalid")
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise PermanentImageEnhancementProviderError("output format is invalid")
    if size not in SUPPORTED_IMAGE_SIZES:
        raise PermanentImageEnhancementProviderError("image size is invalid")
    if background not in SUPPORTED_BACKGROUNDS:
        raise PermanentImageEnhancementProviderError("image background is invalid")
    return {
        "quality": quality,
        "output_format": output_format,
        "size": size,
        "background": background,
    }


@dataclass(frozen=True)
class ImageEnhancementSource:
    content: bytes = field(repr=False)
    mime_type: str
    checksum_sha256: str


@dataclass(frozen=True)
class ImageEnhancementRequest:
    source: ImageEnhancementSource
    model: str
    prompt: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class ImageEnhancementResult:
    output_bytes: bytes = field(repr=False)
    output_format_hint: str | None
    usage: dict[str, int | str]


class ImageEnhancementProvider(Protocol):
    name: str

    def enhance(self, request: ImageEnhancementRequest) -> ImageEnhancementResult: ...


class ImageEnhancementProviderError(RuntimeError):
    pass


class RetryableImageEnhancementProviderError(ImageEnhancementProviderError):
    pass


class PermanentImageEnhancementProviderError(
    ImageEnhancementProviderError,
    PermanentJobError,
):
    pass
