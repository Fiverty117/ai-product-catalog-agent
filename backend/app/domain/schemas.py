import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.domain.enums import (
    ExtractionReviewDecision,
    ExtractionReviewField,
    FieldSource,
    FieldState,
    ExtractionRunStatus,
    JobStatus,
    ObservationState,
    PhotoRole,
    SKUFieldName,
)


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]
CurrencyCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Za-z]{3}$",
    ),
]
ObservationText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]
ObservationSize = Annotated[
    Decimal,
    Field(gt=0, max_digits=18, decimal_places=6),
    WithJsonSchema({"type": "number", "exclusiveMinimum": 0}),
]
ObservationConfidence = Annotated[
    Decimal,
    Field(ge=0, le=1, max_digits=7, decimal_places=6),
    WithJsonSchema({"type": "number", "minimum": 0, "maximum": 1}),
]
ObservationServings = Annotated[int, Field(gt=0)]
ObservationValue = TypeVar("ObservationValue")


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldObservation(StrictSchema, Generic[ObservationValue]):
    value: ObservationValue | None
    confidence: ObservationConfidence | None = None
    evidence: str | None = None
    state: ObservationState

    @model_validator(mode="after")
    def validate_value_for_state(self):
        if self.state is ObservationState.EXTRACTED and self.value is None:
            raise ValueError("extracted observations require a value")
        if self.state is not ObservationState.EXTRACTED and self.value is not None:
            raise ValueError("not_legible and not_present observations require value=null")
        return self


class ProductExtractionResult(StrictSchema):
    brand_name: FieldObservation[ObservationText]
    product_name: FieldObservation[ObservationText]
    flavor: FieldObservation[ObservationText]
    size_value: FieldObservation[ObservationSize]
    size_unit: FieldObservation[ObservationText]
    servings: FieldObservation[ObservationServings]


class FlavorCorrection(StrictSchema):
    flavor: ObservationText


class SizeCorrection(StrictSchema):
    size_value: ObservationSize
    size_unit: ObservationText


class ServingsCorrection(StrictSchema):
    servings: Annotated[int, Field(strict=True, gt=0)]


ReviewCorrection = FlavorCorrection | SizeCorrection | ServingsCorrection


class ExtractionFieldReviewRequest(StrictSchema):
    extraction_run_id: uuid.UUID
    sku_id: uuid.UUID
    field_key: ExtractionReviewField
    decision: ExtractionReviewDecision
    corrected_value: ReviewCorrection | None = None
    replace_locked: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_decision_contract(self):
        expected_correction = {
            ExtractionReviewField.FLAVOR: FlavorCorrection,
            ExtractionReviewField.SIZE: SizeCorrection,
            ExtractionReviewField.SERVINGS: ServingsCorrection,
        }[self.field_key]
        if self.decision is ExtractionReviewDecision.CORRECTED:
            if not isinstance(self.corrected_value, expected_correction):
                raise ValueError(
                    f"corrected {self.field_key.value} review requires its typed value"
                )
        elif self.corrected_value is not None:
            raise ValueError("only corrected reviews may include corrected_value")
        if self.decision is ExtractionReviewDecision.REJECTED and self.replace_locked:
            raise ValueError("rejected reviews cannot replace locked fields")
        return self


class ProductExtractionJobPayload(StrictSchema):
    photo_ids: list[uuid.UUID] = Field(min_length=1)
    provider: NonEmptyText
    model: NonEmptyText
    prompt_version: NonEmptyText
    schema_version: NonEmptyText
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("photo_ids")
    @classmethod
    def require_unique_photos(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("photo_ids must be unique")
        return value

    @field_validator("parameters")
    @classmethod
    def reject_sensitive_parameters(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        sensitive_keys = {
            "api_key",
            "apikey",
            "authorization",
            "access_token",
            "bearer_token",
            "password",
            "secret",
            "file_path",
            "image_path",
        }

        def inspect_item(item: JsonValue) -> None:
            if isinstance(item, dict):
                for key, nested_item in item.items():
                    normalized_key = key.casefold().replace("-", "_")
                    if normalized_key in sensitive_keys:
                        raise ValueError(
                            f"sensitive or path parameter is not allowed: {key}"
                        )
                    inspect_item(nested_item)
            elif isinstance(item, list):
                for nested_item in item:
                    inspect_item(nested_item)

        inspect_item(value)
        return value


class ReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BrandCreate(BaseModel):
    name: NonEmptyText


class BrandRead(ReadSchema):
    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class ProductCreate(BaseModel):
    brand_id: uuid.UUID
    name: NonEmptyText


class ProductRead(ReadSchema):
    id: uuid.UUID
    brand_id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class SKUCreate(BaseModel):
    product_id: uuid.UUID
    external_sku: NonEmptyText | None = None
    flavor: NonEmptyText | None = None
    size_value: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    size_unit: NonEmptyText | None = None
    servings: int | None = Field(default=None, gt=0)

    @field_validator("size_unit")
    @classmethod
    def require_size_value_with_unit(cls, value: str | None, info):
        if value is not None and info.data.get("size_value") is None:
            raise ValueError("size_unit requires size_value")
        return value

    def model_post_init(self, __context: object) -> None:
        if self.size_value is not None and self.size_unit is None:
            raise ValueError("size_value requires size_unit")


class SKURead(ReadSchema):
    id: uuid.UUID
    product_id: uuid.UUID
    external_sku: str | None
    flavor: str | None
    size_value: Decimal | None
    size_unit: str | None
    servings: int | None
    created_at: datetime
    updated_at: datetime


class SKUFieldProvenanceCreate(BaseModel):
    sku_id: uuid.UUID
    field_name: SKUFieldName
    source: FieldSource
    confidence: Decimal | None = Field(
        default=None, ge=0, le=1, max_digits=7, decimal_places=6
    )
    evidence: str | None = None
    state: FieldState
    locked: bool = False


class SKUFieldProvenanceRead(ReadSchema):
    id: uuid.UUID
    sku_id: uuid.UUID
    extraction_run_id: uuid.UUID | None
    field_name: SKUFieldName
    source: FieldSource
    confidence: Decimal | None
    evidence: str | None
    state: FieldState
    locked: bool
    created_at: datetime
    updated_at: datetime


class ExtractionFieldReviewRead(ReadSchema):
    id: uuid.UUID
    extraction_run_id: uuid.UUID
    sku_id: uuid.UUID
    field_key: ExtractionReviewField
    decision: ExtractionReviewDecision
    corrected_value: dict[str, JsonValue] | None
    created_at: datetime
    applied_at: datetime | None


class PhotoCreate(BaseModel):
    sku_id: uuid.UUID | None = None
    file_path: NonEmptyText
    checksum_sha256: Sha256
    original_filename: NonEmptyText
    mime_type: str
    file_size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    role: PhotoRole = PhotoRole.OTHER
    is_original: bool


class PhotoRead(ReadSchema):
    id: uuid.UUID
    sku_id: uuid.UUID | None
    file_path: str
    checksum_sha256: str
    original_filename: str | None
    mime_type: str | None
    file_size_bytes: int | None
    width: int | None
    height: int | None
    role: PhotoRole
    is_original: bool
    created_at: datetime


class PriceCreate(BaseModel):
    sku_id: uuid.UUID
    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    currency: CurrencyCode
    valid_from: datetime
    source: NonEmptyText
    approved: bool = False

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("valid_from")
    @classmethod
    def require_aware_valid_from(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("valid_from must be timezone-aware")
        return value


class PriceRead(ReadSchema):
    id: uuid.UUID
    sku_id: uuid.UUID
    amount: Decimal
    currency: str
    valid_from: datetime
    source: str
    approved: bool
    created_at: datetime


class JobCreate(BaseModel):
    job_type: NonEmptyText
    payload: dict[str, Any]
    idempotency_key: NonEmptyText
    max_attempts: int = Field(default=3, ge=1)


class JobRead(ReadSchema):
    id: uuid.UUID
    job_type: str
    status: JobStatus
    payload: dict[str, Any]
    idempotency_key: str
    attempts: int
    max_attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ExtractionRunRead(ReadSchema):
    id: uuid.UUID
    job_id: uuid.UUID | None
    sku_id: uuid.UUID | None
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    parameters_hash: str
    status: ExtractionRunStatus
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    structured_result: ProductExtractionResult | None
    usage: dict[str, JsonValue] | None
    sanitized_error: str | None
    photos: list[PhotoRead]
