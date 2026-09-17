import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationInfo,
    WithJsonSchema,
    field_serializer,
    field_validator,
    model_validator,
)

from app.domain.enums import (
    CatalogHeroPhotoSource,
    CatalogReadinessIssueCode,
    CatalogReadinessIssueScope,
    CatalogReadinessIssueSeverity,
    CategorySuggestionReviewDecision,
    DerivedImageReviewDecision,
    DerivedImageReviewState,
    ExtractionReviewDecision,
    ExtractionReviewField,
    FieldSource,
    FieldState,
    ExtractionRunStatus,
    JobStatus,
    IdentityResolutionAction,
    ObservationState,
    PhotoPresentationAssetType,
    PhotoPresentationWarning,
    PhotoRole,
    SKUFieldName,
)
from app.domain.identity import clean_identity_display_name


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
CategorySuggestionConfidence = Annotated[
    Decimal,
    Field(ge=0, le=1, max_digits=7, decimal_places=6),
    WithJsonSchema({"type": "number", "minimum": 0, "maximum": 1}),
]
CategoryEvidence = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
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


class UseExistingBrandDecision(StrictSchema):
    action: Literal["use_existing"]
    brand_id: uuid.UUID


class CreateNewBrandDecision(StrictSchema):
    action: Literal["create_new"]
    name: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return clean_identity_display_name(value)


BrandIdentityDecision = Annotated[
    UseExistingBrandDecision | CreateNewBrandDecision,
    Field(discriminator="action"),
]


class UseExistingProductDecision(StrictSchema):
    action: Literal["use_existing"]
    product_id: uuid.UUID


class CreateNewProductDecision(StrictSchema):
    action: Literal["create_new"]
    name: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return clean_identity_display_name(value)


ProductIdentityDecision = Annotated[
    UseExistingProductDecision | CreateNewProductDecision,
    Field(discriminator="action"),
]


class ExtractionIdentityResolutionRequest(StrictSchema):
    extraction_run_id: uuid.UUID
    brand: BrandIdentityDecision
    product: ProductIdentityDecision


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
        return _reject_sensitive_parameters(value)


class CategorySuggestionProductSnapshot(StrictSchema):
    product_id: uuid.UUID
    product_name: ObservationText


class CategorySuggestionBrandSnapshot(StrictSchema):
    brand_id: uuid.UUID
    brand_name: ObservationText


class CategorySuggestionSKUSnapshot(StrictSchema):
    sku_id: uuid.UUID
    flavor: ObservationText | None = None
    size_value: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    size_unit: ObservationText | None = None
    servings: int | None = Field(default=None, gt=0)


class CategoryTaxonomySnapshot(StrictSchema):
    category_id: uuid.UUID
    name: ObservationText
    identity_key: ObservationText
    sort_order: int = Field(strict=True, ge=0)


class CategorySuggestionInputSnapshot(StrictSchema):
    product: CategorySuggestionProductSnapshot
    brand: CategorySuggestionBrandSnapshot
    sku_variants: list[CategorySuggestionSKUSnapshot]
    taxonomy: list[CategoryTaxonomySnapshot]


class CategorySuggestionJobPayload(StrictSchema):
    product_id: uuid.UUID
    provider: NonEmptyText
    model: NonEmptyText
    prompt_version: NonEmptyText
    schema_version: NonEmptyText
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    input_hash: Sha256
    input_snapshot: CategorySuggestionInputSnapshot

    @field_validator("parameters")
    @classmethod
    def reject_sensitive_parameters(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        return _reject_sensitive_parameters(value)


class ImageEnhancementJobPayload(StrictSchema):
    source_photo_id: uuid.UUID
    source_checksum_sha256: Sha256
    provider: NonEmptyText
    model: NonEmptyText
    prompt_version: NonEmptyText
    config_version: NonEmptyText
    parameters: dict[str, JsonValue]

    @field_validator("parameters")
    @classmethod
    def reject_sensitive_parameters(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        return _reject_sensitive_parameters(value)


class DerivedImageReviewCreate(StrictSchema):
    derived_image_id: uuid.UUID
    decision: DerivedImageReviewDecision


class PhotoPresentationSelection(StrictSchema):
    photo_id: uuid.UUID
    derived_image_id: uuid.UUID


class EffectivePhotoPresentation(StrictSchema):
    source_photo_id: uuid.UUID
    asset_type: PhotoPresentationAssetType
    derived_image_id: uuid.UUID | None
    file_path: NonEmptyText
    checksum_sha256: Sha256
    mime_type: str | None
    width: int | None
    height: int | None
    backing_asset_available: bool
    warnings: list[PhotoPresentationWarning]


NonEmptyUUIDList = Annotated[list[uuid.UUID], Field(min_length=1)]
FrozenMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
FrozenSize = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6)]
FrozenServings = Annotated[int, Field(gt=0)]


class CatalogSnapshotCreate(StrictSchema):
    product_ids: NonEmptyUUIDList
    currency: CurrencyCode
    as_of: datetime | None = None
    sku_selection: dict[uuid.UUID, NonEmptyUUIDList] | None = None

    @field_validator("product_ids")
    @classmethod
    def require_unique_products(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("product_ids must be unique")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("as_of must be timezone-aware")
        return value

    @field_validator("sku_selection")
    @classmethod
    def require_unique_sku_selections(
        cls,
        value: dict[uuid.UUID, list[uuid.UUID]] | None,
    ) -> dict[uuid.UUID, list[uuid.UUID]] | None:
        if value is not None:
            for sku_ids in value.values():
                if len(sku_ids) != len(set(sku_ids)):
                    raise ValueError("selected SKU IDs must be unique per Product")
        return value

    @model_validator(mode="after")
    def require_selected_products_for_sku_mapping(self):
        if self.sku_selection is not None:
            unknown = set(self.sku_selection) - set(self.product_ids)
            if unknown:
                raise ValueError(
                    "sku_selection keys must be present in product_ids"
                )
        return self


class FrozenCatalogAsset(StrictSchema):
    checksum_sha256: Sha256
    mime_type: NonEmptyText
    file_size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    storage_relative_path: NonEmptyText

    @model_validator(mode="after")
    def require_content_addressed_relative_path(self):
        parts = self.storage_relative_path.split("/")
        expected_extensions = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        if (
            len(parts) != 3
            or parts[0] not in {"originals", "processed"}
            or parts[1] != self.checksum_sha256[:2].lower()
            or parts[2]
            != self.checksum_sha256.lower()
            + expected_extensions.get(self.mime_type, "")
        ):
            raise ValueError(
                "storage_relative_path must be a canonical content-addressed path"
            )
        return self


class FrozenCatalogPrice(StrictSchema):
    source_price_id: uuid.UUID
    amount: FrozenMoney
    currency: CurrencyCode
    valid_from: datetime
    created_at: datetime
    source: NonEmptyText

    @field_validator("amount", mode="before")
    @classmethod
    def reject_non_decimal_amount(cls, value):
        if not isinstance(value, (Decimal, str)):
            raise ValueError("frozen Decimal values must use Decimal or string input")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("valid_from", "created_at")
    @classmethod
    def require_aware_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("frozen Price timestamps must be timezone-aware")
        return value

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return _canonical_decimal_string(value)


class CatalogVariantSnapshot(StrictSchema):
    source_sku_id: uuid.UUID
    external_sku: str | None
    flavor: str | None
    size_value: FrozenSize | None
    size_unit: str | None
    servings: FrozenServings | None
    price: FrozenCatalogPrice

    @field_validator("size_value", mode="before")
    @classmethod
    def reject_non_decimal_size(cls, value):
        if value is not None and not isinstance(value, (Decimal, str)):
            raise ValueError("frozen Decimal values must use Decimal or string input")
        return value

    @field_serializer("size_value")
    def serialize_size_value(self, value: Decimal | None) -> str | None:
        return _canonical_decimal_string(value) if value is not None else None

    @model_validator(mode="after")
    def require_complete_size_pair(self):
        if (self.size_value is None) != (self.size_unit is None):
            raise ValueError("size_value and size_unit must be set together")
        return self


class CatalogHeroSnapshot(StrictSchema):
    source_photo_id: uuid.UUID
    source_photo_owner_type: CatalogHeroPhotoSource
    source_photo_owner_sku_id: uuid.UUID | None
    source_original_asset: FrozenCatalogAsset
    presentation_type: PhotoPresentationAssetType
    source_derived_image_id: uuid.UUID | None
    presentation_asset: FrozenCatalogAsset

    @model_validator(mode="after")
    def require_derived_id_for_derived_presentation(self):
        if (
            self.presentation_type is PhotoPresentationAssetType.DERIVED
            and self.source_derived_image_id is None
        ):
            raise ValueError("derived presentation requires source_derived_image_id")
        if (
            self.presentation_type is PhotoPresentationAssetType.ORIGINAL
            and self.source_derived_image_id is not None
        ):
            raise ValueError("original presentation cannot reference DerivedImage")
        if (
            self.presentation_type is PhotoPresentationAssetType.ORIGINAL
            and self.presentation_asset != self.source_original_asset
        ):
            raise ValueError("original presentation asset must equal source asset")
        if not self.source_original_asset.storage_relative_path.startswith(
            "originals/"
        ):
            raise ValueError("source original asset must use originals storage")
        expected_area = (
            "processed/"
            if self.presentation_type is PhotoPresentationAssetType.DERIVED
            else "originals/"
        )
        if not self.presentation_asset.storage_relative_path.startswith(expected_area):
            raise ValueError("presentation asset uses the wrong storage area")
        return self


class CatalogProductSnapshot(StrictSchema):
    source_brand_id: uuid.UUID
    brand_name: NonEmptyText
    brand_identity_key: NonEmptyText
    source_product_id: uuid.UUID
    product_name: NonEmptyText
    product_identity_key: NonEmptyText
    hero: CatalogHeroSnapshot
    variants: Annotated[list[CatalogVariantSnapshot], Field(min_length=1)]


class FrozenCatalogCategory(StrictSchema):
    source_category_id: uuid.UUID
    name: NonEmptyText
    identity_key: NonEmptyText
    sort_order: int = Field(ge=0)


class CatalogSectionSnapshot(StrictSchema):
    category: FrozenCatalogCategory
    products: Annotated[list[CatalogProductSnapshot], Field(min_length=1)]


class CatalogSnapshotData(StrictSchema):
    schema_version: Literal["catalog-snapshot-v1"]
    currency: CurrencyCode
    as_of: datetime
    sections: Annotated[list[CatalogSectionSnapshot], Field(min_length=1)]

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_unique_content_and_currency(self):
        category_ids: list[uuid.UUID] = []
        product_ids: list[uuid.UUID] = []
        sku_ids: list[uuid.UUID] = []
        for section in self.sections:
            category_ids.append(section.category.source_category_id)
            for product in section.products:
                product_ids.append(product.source_product_id)
                for variant in product.variants:
                    sku_ids.append(variant.source_sku_id)
                    if variant.price.currency != self.currency:
                        raise ValueError(
                            "frozen Price currency must match snapshot currency"
                        )
        for values, label in (
            (category_ids, "Category"),
            (product_ids, "Product"),
            (sku_ids, "SKU"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"snapshot {label} IDs must be unique")
        return self


class CategorySuggestion(StrictSchema):
    category_id: uuid.UUID
    confidence: CategorySuggestionConfidence
    evidence: CategoryEvidence


class CategorySuggestionResult(StrictSchema):
    primary: CategorySuggestion | None
    secondary: list[CategorySuggestion]

    @model_validator(mode="after")
    def validate_categories(self, info: ValidationInfo):
        secondary_ids = [item.category_id for item in self.secondary]
        if len(secondary_ids) != len(set(secondary_ids)):
            raise ValueError("secondary category IDs must be unique")
        if self.primary is not None and self.primary.category_id in secondary_ids:
            raise ValueError("primary category cannot also be secondary")
        context = info.context or {}
        taxonomy_ids = context.get("taxonomy_ids")
        if taxonomy_ids is not None:
            suggested_ids = set(secondary_ids)
            if self.primary is not None:
                suggested_ids.add(self.primary.category_id)
            unknown_ids = suggested_ids - set(taxonomy_ids)
            if unknown_ids:
                raise ValueError("suggested Category is not in the input taxonomy")
        return self


class CategorySelection(StrictSchema):
    primary_category_id: uuid.UUID | None
    secondary_category_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def validate_categories(self):
        if len(self.secondary_category_ids) != len(set(self.secondary_category_ids)):
            raise ValueError("secondary category IDs must be unique")
        if self.primary_category_id in self.secondary_category_ids:
            raise ValueError("primary category cannot also be secondary")
        return self


class CategorySuggestionReviewRequest(StrictSchema):
    category_suggestion_run_id: uuid.UUID
    decision: CategorySuggestionReviewDecision
    corrected_selection: CategorySelection | None = None
    replace_primary: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_decision_contract(self):
        if self.decision is CategorySuggestionReviewDecision.CORRECTED:
            if self.corrected_selection is None:
                raise ValueError("corrected review requires explicit selection")
        elif self.corrected_selection is not None:
            raise ValueError("only corrected review may include corrected_selection")
        if (
            self.decision is CategorySuggestionReviewDecision.REJECTED
            and self.replace_primary
        ):
            raise ValueError("rejected review cannot replace the primary Category")
        return self


def _reject_sensitive_parameters(
    value: dict[str, JsonValue],
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
    identity_key: str
    created_at: datetime
    updated_at: datetime


class ProductCreate(BaseModel):
    brand_id: uuid.UUID
    name: NonEmptyText


class ProductRead(ReadSchema):
    id: uuid.UUID
    brand_id: uuid.UUID
    name: str
    identity_key: str
    created_at: datetime
    updated_at: datetime


class CategoryCreate(StrictSchema):
    name: str
    sort_order: int = Field(default=1000, strict=True, ge=0)
    is_active: bool = Field(default=True, strict=True)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return clean_identity_display_name(value)


class CategoryRead(ReadSchema):
    id: uuid.UUID
    name: str
    identity_key: str
    sort_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProductCategoryRead(ReadSchema):
    id: uuid.UUID
    product_id: uuid.UUID
    category_id: uuid.UUID
    category_suggestion_run_id: uuid.UUID | None
    is_primary: bool
    source: FieldSource
    verified: bool
    locked: bool
    created_at: datetime
    updated_at: datetime


class CategorySuggestionRunRead(ReadSchema):
    id: uuid.UUID
    product_id: uuid.UUID
    job_id: uuid.UUID | None
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    parameters: dict[str, JsonValue]
    input_hash: str
    input_snapshot: CategorySuggestionInputSnapshot
    status: ExtractionRunStatus
    structured_result: CategorySuggestionResult | None
    usage: dict[str, JsonValue] | None
    sanitized_error: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class CategorySuggestionReviewRead(ReadSchema):
    id: uuid.UUID
    category_suggestion_run_id: uuid.UUID
    decision: CategorySuggestionReviewDecision
    final_selection: CategorySelection | None
    created_at: datetime
    applied_at: datetime | None


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


class ExtractionIdentityResolutionRead(ReadSchema):
    id: uuid.UUID
    extraction_run_id: uuid.UUID
    brand_id: uuid.UUID
    product_id: uuid.UUID
    brand_action: IdentityResolutionAction
    product_action: IdentityResolutionAction
    created_at: datetime
    applied_at: datetime


class PhotoCreate(BaseModel):
    sku_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    file_path: NonEmptyText
    checksum_sha256: Sha256
    original_filename: NonEmptyText
    mime_type: str
    file_size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    role: PhotoRole = PhotoRole.OTHER
    is_original: bool

    @model_validator(mode="after")
    def require_single_owner(self):
        if self.sku_id is not None and self.product_id is not None:
            raise ValueError("Photo cannot belong to both a Product and an SKU")
        return self


class PhotoRead(ReadSchema):
    id: uuid.UUID
    sku_id: uuid.UUID | None
    product_id: uuid.UUID | None
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


class ProductCatalogReadinessRequest(StrictSchema):
    product_id: uuid.UUID
    currency: CurrencyCode
    as_of: datetime | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("as_of must be timezone-aware")
        return value


class CatalogReadinessIssue(StrictSchema):
    code: CatalogReadinessIssueCode
    severity: CatalogReadinessIssueSeverity
    scope: CatalogReadinessIssueScope
    message: NonEmptyText
    product_id: uuid.UUID | None = None
    sku_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    photo_id: uuid.UUID | None = None


class SKUCatalogReadiness(StrictSchema):
    sku_id: uuid.UUID
    is_publishable: bool
    active_price_id: uuid.UUID | None
    active_price_amount: Decimal | None
    active_price_currency: str | None
    active_price_valid_from: datetime | None
    blockers: list[CatalogReadinessIssue]


class ProductCatalogReadiness(StrictSchema):
    product_id: uuid.UUID
    currency: str
    as_of: datetime
    is_ready: bool
    primary_category_id: uuid.UUID | None
    hero_photo_id: uuid.UUID | None
    hero_photo_source: CatalogHeroPhotoSource | None
    hero_source_sku_id: uuid.UUID | None
    hero_presentation_type: PhotoPresentationAssetType | None
    hero_derived_image_id: uuid.UUID | None
    presentation_warnings: list[PhotoPresentationWarning]
    ready_sku_ids: list[uuid.UUID]
    sku_reports: list[SKUCatalogReadiness]
    blockers: list[CatalogReadinessIssue]
    warnings: list[CatalogReadinessIssue]


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


class ImageEnhancementRunRead(ReadSchema):
    id: uuid.UUID
    source_photo_id: uuid.UUID
    job_id: uuid.UUID | None
    provider: str
    model: str
    prompt_version: str
    config_version: str
    parameters_hash: str
    status: ExtractionRunStatus
    usage: dict[str, JsonValue] | None
    sanitized_error: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class DerivedImageRead(ReadSchema):
    id: uuid.UUID
    source_photo_id: uuid.UUID
    enhancement_run_id: uuid.UUID
    file_path: str
    checksum_sha256: str
    mime_type: str
    file_size_bytes: int
    width: int
    height: int
    created_at: datetime


class DerivedImageReviewRead(ReadSchema):
    id: uuid.UUID
    derived_image_id: uuid.UUID
    decision: DerivedImageReviewDecision
    created_at: datetime


class PhotoPresentationPreferenceRead(ReadSchema):
    id: uuid.UUID
    photo_id: uuid.UUID
    selected_derived_image_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class DerivedImageReviewStateRead(StrictSchema):
    derived_image_id: uuid.UUID
    state: DerivedImageReviewState
    current_review: DerivedImageReviewRead | None


class CatalogSnapshotRead(ReadSchema):
    id: uuid.UUID
    schema_version: str
    currency: str
    as_of: datetime
    payload: CatalogSnapshotData
    content_hash: str
    created_at: datetime


class CatalogRenderConfig(StrictSchema):
    locale: NonEmptyText = "es-PY"
    page_size: Literal["A4"] = "A4"
    orientation: Literal["portrait", "landscape"] = "portrait"
    template_key: NonEmptyText = "grabelan-catalog-v1"


class CatalogRenderJobPayload(StrictSchema):
    catalog_snapshot_id: uuid.UUID
    snapshot_content_hash: Sha256
    snapshot_schema_version: NonEmptyText
    template_key: NonEmptyText
    template_version: NonEmptyText
    template_hash: Sha256
    renderer_version: NonEmptyText
    config: CatalogRenderConfig
    config_hash: Sha256


CatalogBrandKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=100)]
CatalogBrandColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]


class CatalogBrandProfileCreate(StrictSchema):
    key: CatalogBrandKey
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    primary_color: CatalogBrandColor
    accent_color: CatalogBrandColor
    contact_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None
    social_handle: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None

    @field_validator("primary_color", "accent_color")
    @classmethod
    def canonical_color(cls, value: str) -> str:
        return value.upper()


class CatalogBrandProfileUpdate(StrictSchema):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    primary_color: CatalogBrandColor | None = None
    accent_color: CatalogBrandColor | None = None
    contact_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None
    social_handle: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    is_active: bool | None = None

    @field_validator("primary_color", "accent_color")
    @classmethod
    def canonical_color(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def required_fields_cannot_be_cleared(self):
        if any(name in self.model_fields_set and getattr(self, name) is None for name in ("display_name", "primary_color", "accent_color", "is_active")):
            raise ValueError("required catalog brand profile fields cannot be cleared")
        return self


class FrozenCatalogBrandLogo(StrictSchema):
    source_brand_asset_id: uuid.UUID
    checksum_sha256: Sha256
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    file_size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    storage_relative_path: Annotated[str, StringConstraints(pattern=r"^branding/[0-9a-f]{2}/[0-9a-f]{64}\.(?:png|jpg|webp)$")]

    @model_validator(mode="after")
    def validate_locator(self):
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[self.mime_type]
        if self.storage_relative_path != f"branding/{self.checksum_sha256[:2].lower()}/{self.checksum_sha256.lower()}{suffix}":
            raise ValueError("branding logo locator must match frozen content identity")
        return self


class ResolvedCatalogBranding(StrictSchema):
    schema_version: Literal["catalog-branding-v1"]
    source_profile_id: uuid.UUID
    profile_key: CatalogBrandKey
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    primary_color: CatalogBrandColor
    accent_color: CatalogBrandColor
    contact_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None
    social_handle: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    logo: FrozenCatalogBrandLogo | None = None

    @field_validator("primary_color", "accent_color")
    @classmethod
    def canonical_color(cls, value: str) -> str:
        return value.upper()


class CatalogRenderJobPayloadV2(CatalogRenderJobPayload):
    catalog_brand_profile_id: uuid.UUID
    branding_schema_version: Literal["catalog-branding-v1"]
    branding_hash: Sha256
    branding_data: ResolvedCatalogBranding

    @model_validator(mode="after")
    def validate_brand_lineage(self):
        if self.branding_data.source_profile_id != self.catalog_brand_profile_id or self.branding_data.schema_version != self.branding_schema_version:
            raise ValueError("frozen branding lineage does not match render Job")
        return self


class CatalogBrandingView(StrictSchema):
    display_name: NonEmptyText
    logo_data_uri: Annotated[str, StringConstraints(pattern=r"^data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$")] | None = None
    primary_color: CatalogBrandColor
    accent_color: CatalogBrandColor
    contact_text: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    social_handle: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None


class CatalogRenderVariantView(StrictSchema):
    source_sku_id: uuid.UUID
    label: str | None
    price_display: NonEmptyText


class CatalogRenderProductView(StrictSchema):
    source_product_id: uuid.UUID
    brand_name: NonEmptyText
    product_name: NonEmptyText
    image_data_uri: NonEmptyText
    variants: Annotated[list[CatalogRenderVariantView], Field(min_length=1)]


class CatalogRenderSectionView(StrictSchema):
    source_category_id: uuid.UUID
    category_name: NonEmptyText
    products: Annotated[list[CatalogRenderProductView], Field(min_length=1)]


class CatalogRenderViewModel(StrictSchema):
    locale: NonEmptyText
    page_size: Literal["A4"]
    orientation: Literal["portrait", "landscape"]
    store_name: NonEmptyText
    branding: CatalogBrandingView | None = None
    title: NonEmptyText
    as_of_label: NonEmptyText
    currency: CurrencyCode
    sections: Annotated[list[CatalogRenderSectionView], Field(min_length=1)]


class CatalogRenderRunRead(ReadSchema):
    id: uuid.UUID
    catalog_snapshot_id: uuid.UUID
    job_id: uuid.UUID | None
    template_key: str
    template_version: str
    template_hash: str
    renderer_version: str
    renderer_engine: str
    renderer_engine_version: str | None
    locale: str
    config_hash: str
    catalog_brand_profile_id: uuid.UUID | None
    branding_schema_version: str | None
    branding_hash: str | None
    branding_data: ResolvedCatalogBranding | None
    status: ExtractionRunStatus
    sanitized_error: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class CatalogArtifactRead(ReadSchema):
    id: uuid.UUID
    catalog_snapshot_id: uuid.UUID
    render_run_id: uuid.UUID
    media_type: str
    file_path: str
    checksum_sha256: str
    file_size_bytes: int
    page_count: int
    created_at: datetime


def _canonical_decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
