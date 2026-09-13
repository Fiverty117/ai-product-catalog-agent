from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.domain.schemas import ProductExtractionResult
from app.services.jobs import PermanentJobError


@dataclass(frozen=True)
class VisionImage:
    content: bytes = field(repr=False)
    mime_type: str
    checksum_sha256: str


@dataclass(frozen=True)
class VisionExtractionRequest:
    model: str
    prompt: str
    images: tuple[VisionImage, ...]
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class VisionExtractionResponse:
    structured_result: ProductExtractionResult
    usage: dict[str, int | str | None]


class VisionProvider(Protocol):
    name: str

    def extract(self, request: VisionExtractionRequest) -> VisionExtractionResponse: ...


class VisionProviderError(RuntimeError):
    pass


class RetryableVisionProviderError(VisionProviderError):
    pass


class PermanentVisionProviderError(VisionProviderError, PermanentJobError):
    pass
