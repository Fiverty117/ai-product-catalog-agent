"""Typed, versioned, non-canonical product intake draft."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.schemas import CatalogBuilderProductSummary, ProductExtractionResult


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


class IntakeSKU(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flavor: str | None = Field(default=None, max_length=255)
    size_value: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    size_unit: str | None = Field(default=None, max_length=100)
    servings: int | None = Field(default=None, strict=True, gt=0)
    external_sku: str | None = Field(default=None, max_length=255)

    @field_validator("flavor", "size_unit", "external_sku")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @model_validator(mode="after")
    def size_pair(self):
        if (self.size_value is None) != (self.size_unit is None):
            raise ValueError("size_value and size_unit must be provided together")
        return self


class ProductIntakeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    brand_name: str | None = Field(default=None, max_length=255)
    product_name: str | None = Field(default=None, max_length=255)
    primary_category_name: str | None = Field(default=None, max_length=255)
    secondary_category_names: list[str] = Field(default_factory=list)
    skus: list[IntakeSKU] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("brand_name", "product_name", "primary_category_name", "notes")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("secondary_category_names")
    @classmethod
    def clean_categories(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 255 for value in cleaned):
            raise ValueError("secondary categories must be non-empty and at most 255 characters")
        return cleaned


class IntakePhotoRead(BaseModel):
    id: UUID
    position: int
    is_primary: bool
    original_filename: str
    mime_type: str
    image_url: str


class IntakeExtractionRead(BaseModel):
    job_id: UUID | None
    job_status: str | None
    attempts: int | None
    max_attempts: int | None
    run_id: UUID | None
    run_status: str | None
    error: str | None
    newer_result_available: bool
    observation: ProductExtractionResult | None


class ProductIntakeRead(BaseModel):
    id: UUID
    status: Literal["draft", "queued", "running", "review_required", "failed", "promoted"]
    created_at: datetime
    updated_at: datetime
    draft: ProductIntakeDraft
    human_edited: bool
    photos: list[IntakePhotoRead]
    extraction: IntakeExtractionRead
    promotion: "IntakePromotionLink | None"


class ProductIntakeList(BaseModel):
    items: list[ProductIntakeRead]


class PrimaryPhotoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    photo_id: UUID


class IntakePromotionLink(BaseModel):
    product_id: UUID
    promoted_at: datetime


class PromotionPriceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intake_sku_index: int = Field(ge=0, strict=True)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    currency: str = Field(default="PYG", pattern=r"^[A-Z]{3}$")

    @field_validator("amount", mode="before")
    @classmethod
    def require_decimal_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("price amount must be a decimal string")
        return value


class ProductIntakePromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    primary_category_id: UUID | None = None
    secondary_category_ids: list[UUID] = Field(default_factory=list)
    sku_prices: list[PromotionPriceInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_choices(self):
        category_ids = ([self.primary_category_id] if self.primary_category_id else []) + self.secondary_category_ids
        if len(category_ids) != len(set(category_ids)):
            raise ValueError("category selections must be unique")
        indices = [price.intake_sku_index for price in self.sku_prices]
        if len(indices) != len(set(indices)):
            raise ValueError("one initial price per variant is allowed")
        return self


class PromotionCategoryOption(BaseModel):
    id: UUID
    name: str


class ProductIntakePromotionResult(BaseModel):
    status: Literal["promoted"] = "promoted"
    readiness_currency: Literal["PYG"] = "PYG"
    intake_id: UUID
    product_id: UUID
    brand_id: UUID
    brand_name: str
    brand_reused: bool
    promoted_at: datetime
    sku_ids: list[UUID]
    product: CatalogBuilderProductSummary


class ProductIntakePromotionContext(BaseModel):
    categories: list[PromotionCategoryOption]
    result: ProductIntakePromotionResult | None
