import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Enum,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base
from app.db.types import UTCDateTime, utc_now
from app.domain.enums import (
    CategorySuggestionReviewDecision,
    DerivedImageReviewDecision,
    ExtractionReviewDecision,
    ExtractionReviewField,
    ExtractionRunStatus,
    FieldSource,
    FieldState,
    IdentityResolutionAction,
    JobStatus,
    PhotoRole,
    ProductCopyReviewDecision,
    ProductCopyType,
    SKUFieldName,
)
from app.domain.identity import identity_key_v1


extraction_run_photos = Table(
    "extraction_run_photos",
    Base.metadata,
    Column(
        "extraction_run_id",
        Uuid(as_uuid=True),
        ForeignKey("extraction_runs.id"),
        primary_key=True,
    ),
    Column(
        "photo_id",
        Uuid(as_uuid=True),
        ForeignKey("photos.id"),
        primary_key=True,
    ),
    Index("ix_extraction_run_photos_photo_id", "photo_id"),
)


class Brand(Base):
    __tablename__ = "brands"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_brands_name_nonempty"),
        CheckConstraint(
            "length(identity_key) > 0",
            name="ck_brands_identity_key_nonempty",
        ),
        UniqueConstraint("identity_key", name="uq_brands_identity_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    products: Mapped[list["Product"]] = relationship(back_populates="brand")
    identity_resolutions: Mapped[list["ExtractionIdentityResolution"]] = relationship(
        back_populates="brand"
    )

    @validates("name")
    def normalize_name(self, _key: str, value: str) -> str:
        cleaned = " ".join(value.split())
        self.identity_key = identity_key_v1(cleaned) if cleaned else ""
        return cleaned


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_products_name_nonempty"),
        CheckConstraint(
            "length(identity_key) > 0",
            name="ck_products_identity_key_nonempty",
        ),
        UniqueConstraint(
            "brand_id",
            "identity_key",
            name="uq_products_brand_identity_key",
        ),
        Index("ix_products_brand_id", "brand_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("brands.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    brand: Mapped[Brand] = relationship(back_populates="products")
    skus: Mapped[list["SKU"]] = relationship(back_populates="product")
    photos: Mapped[list["Photo"]] = relationship(back_populates="product")
    category_assignments: Mapped[list["ProductCategory"]] = relationship(
        back_populates="product"
    )
    category_suggestion_runs: Mapped[list["CategorySuggestionRun"]] = relationship(
        back_populates="product"
    )
    product_copy_runs: Mapped[list["ProductCopyRun"]] = relationship(
        back_populates="product"
    )
    product_copy_manual_revisions: Mapped[list["ProductCopyManualRevision"]] = relationship(
        back_populates="product"
    )
    identity_edits: Mapped[list["ProductIdentityEdit"]] = relationship(back_populates="product")
    identity_resolutions: Mapped[list["ExtractionIdentityResolution"]] = relationship(
        back_populates="product"
    )

    @validates("name")
    def normalize_name(self, _key: str, value: str) -> str:
        cleaned = " ".join(value.split())
        self.identity_key = identity_key_v1(cleaned) if cleaned else ""
        return cleaned


class ProductIdentityEdit(Base):
    __tablename__ = "product_identity_edits"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False, index=True)
    old_name: Mapped[str] = mapped_column(String(255), nullable=False)
    new_name: Mapped[str] = mapped_column(String(255), nullable=False)
    old_brand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("brands.id"), nullable=False)
    new_brand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("brands.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    product: Mapped[Product] = relationship(back_populates="identity_edits")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_categories_name_nonempty"),
        CheckConstraint(
            "length(identity_key) > 0",
            name="ck_categories_identity_key_nonempty",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_categories_sort_order_nonnegative",
        ),
        UniqueConstraint("identity_key", name="uq_categories_identity_key"),
        Index(
            "ix_categories_active_order",
            "is_active",
            "sort_order",
            "identity_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=1000)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    product_assignments: Mapped[list["ProductCategory"]] = relationship(
        back_populates="category"
    )

    @validates("name")
    def normalize_name(self, _key: str, value: str) -> str:
        cleaned = " ".join(value.split())
        self.identity_key = identity_key_v1(cleaned) if cleaned else ""
        return cleaned


class ProductCategory(Base):
    __tablename__ = "product_categories"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "category_id",
            name="uq_product_categories_product_category",
        ),
        Index("ix_product_categories_category_id", "category_id"),
        Index(
            "ix_product_categories_category_suggestion_run_id",
            "category_suggestion_run_id",
        ),
        Index(
            "uq_product_categories_one_primary",
            "product_id",
            unique=True,
            sqlite_where=text("is_primary = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("categories.id"), nullable=False
    )
    category_suggestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("category_suggestion_runs.id")
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[FieldSource] = mapped_column(
        Enum(
            FieldSource,
            name="field_source",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    product: Mapped[Product] = relationship(back_populates="category_assignments")
    category: Mapped[Category] = relationship(back_populates="product_assignments")
    category_suggestion_run: Mapped["CategorySuggestionRun | None"] = relationship(
        back_populates="product_category_assignments"
    )


class SKU(Base):
    __tablename__ = "skus"
    __table_args__ = (
        CheckConstraint("size_value IS NULL OR size_value > 0", name="ck_skus_size_value_positive"),
        CheckConstraint("servings IS NULL OR servings > 0", name="ck_skus_servings_positive"),
        CheckConstraint(
            "(size_value IS NULL AND size_unit IS NULL) OR "
            "(size_value IS NOT NULL AND size_unit IS NOT NULL "
            "AND length(trim(size_unit)) > 0)",
            name="ck_skus_size_value_unit_pair",
        ),
        Index("ix_skus_product_id", "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    external_sku: Mapped[str | None] = mapped_column(String(255))
    flavor: Mapped[str | None] = mapped_column(String(255))
    size_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    size_unit: Mapped[str | None] = mapped_column(String(32))
    servings: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    product: Mapped[Product] = relationship(back_populates="skus")
    photos: Mapped[list["Photo"]] = relationship(back_populates="sku")
    prices: Mapped[list["Price"]] = relationship(back_populates="sku")
    field_provenance: Mapped[list["SKUFieldProvenance"]] = relationship(
        back_populates="sku"
    )
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(
        back_populates="sku"
    )
    extraction_field_reviews: Mapped[list["ExtractionFieldReview"]] = relationship(
        back_populates="sku"
    )


class SKUFieldProvenance(Base):
    __tablename__ = "sku_field_provenance"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_sku_field_provenance_confidence_range",
        ),
        UniqueConstraint(
            "sku_id",
            "field_name",
            name="uq_sku_field_provenance_sku_field",
        ),
        Index("ix_sku_field_provenance_sku_id", "sku_id"),
        Index(
            "ix_sku_field_provenance_extraction_run_id",
            "extraction_run_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sku_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id"), nullable=False
    )
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("extraction_runs.id")
    )
    field_name: Mapped[SKUFieldName] = mapped_column(
        Enum(
            SKUFieldName,
            name="sku_field_name",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    source: Mapped[FieldSource] = mapped_column(
        Enum(
            FieldSource,
            name="field_source",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(7, 6))
    evidence: Mapped[str | None] = mapped_column(Text)
    state: Mapped[FieldState] = mapped_column(
        Enum(
            FieldState,
            name="field_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    sku: Mapped[SKU] = relationship(back_populates="field_provenance")
    extraction_run: Mapped["ExtractionRun | None"] = relationship(
        back_populates="field_provenance"
    )


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (
        CheckConstraint("length(trim(file_path)) > 0", name="ck_photos_file_path_nonempty"),
        CheckConstraint(
            "length(checksum_sha256) = 64 "
            "AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_photos_checksum_sha256_format",
        ),
        CheckConstraint(
            "length(trim(original_filename)) > 0",
            name="ck_photos_original_filename_nonempty",
        ),
        CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_photos_supported_mime_type",
        ),
        CheckConstraint("file_size_bytes > 0", name="ck_photos_file_size_positive"),
        CheckConstraint("width > 0", name="ck_photos_width_positive"),
        CheckConstraint("height > 0", name="ck_photos_height_positive"),
        CheckConstraint(
            "product_id IS NULL OR sku_id IS NULL",
            name="ck_photos_single_owner",
        ),
        Index("ix_photos_product_id", "product_id"),
        Index("ix_photos_sku_id", "sku_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id"), nullable=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id"), nullable=True
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(32))
    file_size_bytes: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    role: Mapped[PhotoRole] = mapped_column(
        Enum(
            PhotoRole,
            name="photo_role",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=PhotoRole.OTHER,
    )
    is_original: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)

    sku: Mapped[SKU | None] = relationship(back_populates="photos")
    product: Mapped[Product | None] = relationship(back_populates="photos")
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(
        secondary=extraction_run_photos,
        back_populates="photos",
    )
    image_enhancement_runs: Mapped[list["ImageEnhancementRun"]] = relationship(
        back_populates="source_photo"
    )
    derived_images: Mapped[list["DerivedImage"]] = relationship(
        back_populates="source_photo"
    )
    presentation_preference: Mapped["PhotoPresentationPreference | None"] = (
        relationship(back_populates="photo", uselist=False)
    )


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_prices_amount_nonnegative"),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency) "
            "AND currency NOT GLOB '*[^A-Z]*'",
            name="ck_prices_currency_iso_code",
        ),
        CheckConstraint("length(trim(source)) > 0", name="ck_prices_source_nonempty"),
        Index("ix_prices_sku_id_valid_from", "sku_id", "valid_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)

    sku: Mapped[SKU] = relationship(back_populates="prices")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("length(trim(job_type)) > 0", name="ck_jobs_job_type_nonempty"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_jobs_idempotency_key_nonempty",
        ),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
        CheckConstraint(
            "attempts <= max_attempts",
            name="ck_jobs_attempts_within_maximum",
        ),
        Index("ix_jobs_claim", "status", "next_retry_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="job_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=JobStatus.QUEUED,
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(
        back_populates="job"
    )
    category_suggestion_runs: Mapped[list["CategorySuggestionRun"]] = relationship(
        back_populates="job"
    )
    image_enhancement_runs: Mapped[list["ImageEnhancementRun"]] = relationship(
        back_populates="job"
    )
    catalog_render_runs: Mapped[list["CatalogRenderRun"]] = relationship(
        back_populates="job"
    )
    product_copy_runs: Mapped[list["ProductCopyRun"]] = relationship(
        back_populates="job"
    )
    catalog_build: Mapped["CatalogBuild | None"] = relationship(
        back_populates="job", uselist=False
    )


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        CheckConstraint("length(trim(provider)) > 0", name="ck_extraction_runs_provider_nonempty"),
        CheckConstraint("length(trim(model)) > 0", name="ck_extraction_runs_model_nonempty"),
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_extraction_runs_prompt_version_nonempty",
        ),
        CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_extraction_runs_schema_version_nonempty",
        ),
        CheckConstraint(
            "length(parameters_hash) = 64 "
            "AND parameters_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_extraction_runs_parameters_hash_format",
        ),
        Index("ix_extraction_runs_job_id", "job_id"),
        Index("ix_extraction_runs_sku_id", "sku_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id")
    )
    sku_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id")
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ExtractionRunStatus] = mapped_column(
        Enum(
            ExtractionRunStatus,
            name="extraction_run_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ExtractionRunStatus.RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    structured_result: Mapped[dict | None] = mapped_column(JSON)
    usage: Mapped[dict | None] = mapped_column(JSON)
    sanitized_error: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job | None] = relationship(back_populates="extraction_runs")
    sku: Mapped[SKU | None] = relationship(back_populates="extraction_runs")
    photos: Mapped[list[Photo]] = relationship(
        secondary=extraction_run_photos,
        back_populates="extraction_runs",
    )
    field_provenance: Mapped[list[SKUFieldProvenance]] = relationship(
        back_populates="extraction_run"
    )
    field_reviews: Mapped[list["ExtractionFieldReview"]] = relationship(
        back_populates="extraction_run"
    )
    identity_resolution: Mapped["ExtractionIdentityResolution | None"] = relationship(
        back_populates="extraction_run",
        uselist=False,
    )


class ProductIntakeItem(Base):
    __tablename__ = "product_intake_items"
    __table_args__ = (Index("ix_product_intake_items_updated_at", "updated_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    draft: Mapped[dict] = mapped_column(JSON, nullable=False)
    human_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    draft_source_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("extraction_runs.id"))
    latest_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"))
    latest_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("extraction_runs.id"))
    latest_action_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now)
    photos: Mapped[list["ProductIntakePhoto"]] = relationship(back_populates="item", order_by="ProductIntakePhoto.position")
    promotion: Mapped["ProductIntakePromotion | None"] = relationship(back_populates="item", uselist=False)


class ProductIntakePhoto(Base):
    __tablename__ = "product_intake_photos"
    __table_args__ = (
        UniqueConstraint("intake_item_id", "position"),
        UniqueConstraint("photo_id"),
    )

    intake_item_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("product_intake_items.id"), primary_key=True)
    photo_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("photos.id"), primary_key=True)
    position: Mapped[int] = mapped_column(nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    item: Mapped[ProductIntakeItem] = relationship(back_populates="photos")
    photo: Mapped[Photo] = relationship()


class ProductIntakePromotion(Base):
    __tablename__ = "product_intake_promotions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_product_intake_promotions_key"),
        UniqueConstraint("product_id", name="uq_product_intake_promotions_product"),
    )

    intake_item_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("product_intake_items.id"), primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    brand_reused: Mapped[bool] = mapped_column(Boolean, nullable=False)
    promoted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)

    item: Mapped[ProductIntakeItem] = relationship(back_populates="promotion")
    product: Mapped[Product] = relationship()


class ExtractionFieldReview(Base):
    __tablename__ = "extraction_field_reviews"
    __table_args__ = (
        CheckConstraint(
            "(decision = 'corrected' AND corrected_value IS NOT NULL) OR "
            "(decision IN ('accepted', 'rejected') AND corrected_value IS NULL)",
            name="ck_extraction_field_reviews_corrected_value",
        ),
        CheckConstraint(
            "(decision = 'rejected' AND applied_at IS NULL) OR "
            "(decision IN ('accepted', 'corrected') AND applied_at IS NOT NULL)",
            name="ck_extraction_field_reviews_applied_at",
        ),
        UniqueConstraint(
            "extraction_run_id",
            "sku_id",
            "field_key",
            name="uq_extraction_field_reviews_run_sku_field",
        ),
        Index(
            "ix_extraction_field_reviews_extraction_run_id",
            "extraction_run_id",
        ),
        Index("ix_extraction_field_reviews_sku_id", "sku_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("extraction_runs.id"), nullable=False
    )
    sku_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id"), nullable=False
    )
    field_key: Mapped[ExtractionReviewField] = mapped_column(
        Enum(
            ExtractionReviewField,
            name="extraction_review_field",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    decision: Mapped[ExtractionReviewDecision] = mapped_column(
        Enum(
            ExtractionReviewDecision,
            name="extraction_review_decision",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    corrected_value: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    extraction_run: Mapped[ExtractionRun] = relationship(
        back_populates="field_reviews"
    )
    sku: Mapped[SKU] = relationship(back_populates="extraction_field_reviews")


class ExtractionIdentityResolution(Base):
    __tablename__ = "extraction_identity_resolutions"
    __table_args__ = (
        UniqueConstraint(
            "extraction_run_id",
            name="uq_extraction_identity_resolutions_run",
        ),
        Index("ix_extraction_identity_resolutions_brand_id", "brand_id"),
        Index("ix_extraction_identity_resolutions_product_id", "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("extraction_runs.id"), nullable=False
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("brands.id"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    brand_action: Mapped[IdentityResolutionAction] = mapped_column(
        Enum(
            IdentityResolutionAction,
            name="brand_identity_resolution_action",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    product_action: Mapped[IdentityResolutionAction] = mapped_column(
        Enum(
            IdentityResolutionAction,
            name="product_identity_resolution_action",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    extraction_run: Mapped[ExtractionRun] = relationship(
        back_populates="identity_resolution"
    )
    brand: Mapped[Brand] = relationship(back_populates="identity_resolutions")
    product: Mapped[Product] = relationship(back_populates="identity_resolutions")


class CategorySuggestionRun(Base):
    __tablename__ = "category_suggestion_runs"
    __table_args__ = (
        CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_category_suggestion_runs_provider_nonempty",
        ),
        CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_category_suggestion_runs_model_nonempty",
        ),
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_category_suggestion_runs_prompt_version_nonempty",
        ),
        CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_category_suggestion_runs_schema_version_nonempty",
        ),
        CheckConstraint(
            "length(input_hash) = 64 "
            "AND input_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_category_suggestion_runs_input_hash_format",
        ),
        Index("ix_category_suggestion_runs_product_id", "product_id"),
        Index("ix_category_suggestion_runs_job_id", "job_id"),
        Index("ix_category_suggestion_runs_input_hash", "input_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id")
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[ExtractionRunStatus] = mapped_column(
        Enum(
            ExtractionRunStatus,
            name="category_suggestion_run_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ExtractionRunStatus.RUNNING,
    )
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sanitized_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    product: Mapped[Product] = relationship(back_populates="category_suggestion_runs")
    job: Mapped[Job | None] = relationship(back_populates="category_suggestion_runs")
    review: Mapped["CategorySuggestionReview | None"] = relationship(
        back_populates="category_suggestion_run",
        uselist=False,
    )
    product_category_assignments: Mapped[list[ProductCategory]] = relationship(
        back_populates="category_suggestion_run"
    )


class CategorySuggestionReview(Base):
    __tablename__ = "category_suggestion_reviews"
    __table_args__ = (
        CheckConstraint(
            "(decision = 'corrected' AND final_selection IS NOT NULL) OR "
            "(decision IN ('accepted', 'rejected') AND final_selection IS NULL)",
            name="ck_category_suggestion_reviews_final_selection",
        ),
        CheckConstraint(
            "(decision = 'rejected' AND applied_at IS NULL) OR "
            "(decision IN ('accepted', 'corrected') AND applied_at IS NOT NULL)",
            name="ck_category_suggestion_reviews_applied_at",
        ),
        UniqueConstraint(
            "category_suggestion_run_id",
            name="uq_category_suggestion_reviews_run",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_suggestion_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("category_suggestion_runs.id"),
        nullable=False,
    )
    decision: Mapped[CategorySuggestionReviewDecision] = mapped_column(
        Enum(
            CategorySuggestionReviewDecision,
            name="category_suggestion_review_decision",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    final_selection: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    category_suggestion_run: Mapped[CategorySuggestionRun] = relationship(
        back_populates="review"
    )


class ProductCopyRun(Base):
    __tablename__ = "product_copy_runs"
    __table_args__ = (
        CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_product_copy_runs_provider_nonempty",
        ),
        CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_product_copy_runs_model_nonempty",
        ),
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_product_copy_runs_prompt_version_nonempty",
        ),
        CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_product_copy_runs_schema_version_nonempty",
        ),
        CheckConstraint(
            "length(source_fingerprint) = 64 "
            "AND source_fingerprint NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_product_copy_runs_source_fingerprint_format",
        ),
        CheckConstraint(
            "generated_text IS NULL OR "
            "(length(trim(generated_text)) > 0 AND length(generated_text) <= 180)",
            name="ck_product_copy_runs_generated_text_length",
        ),
        Index("ix_product_copy_runs_product_id", "product_id"),
        Index("ix_product_copy_runs_job_id", "job_id"),
        Index("ix_product_copy_runs_source_fingerprint", "source_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id")
    )
    copy_type: Mapped[ProductCopyType] = mapped_column(
        Enum(
            ProductCopyType,
            name="product_copy_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[ExtractionRunStatus] = mapped_column(
        Enum(
            ExtractionRunStatus,
            name="product_copy_run_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ExtractionRunStatus.RUNNING,
    )
    generated_text: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sanitized_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    product: Mapped[Product] = relationship(back_populates="product_copy_runs")
    job: Mapped[Job | None] = relationship(back_populates="product_copy_runs")
    review: Mapped["ProductCopyReview | None"] = relationship(
        back_populates="product_copy_run", uselist=False
    )


class ProductCopyManualRevision(Base):
    __tablename__ = "product_copy_manual_revisions"
    __table_args__ = (
        CheckConstraint("length(trim(short_description)) > 0 AND length(short_description) <= 180", name="ck_product_copy_manual_revisions_text"),
        CheckConstraint("length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-fA-F]*'", name="ck_product_copy_manual_revisions_fingerprint"),
        Index("ix_product_copy_manual_revisions_product_id", "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False)
    copy_type: Mapped[ProductCopyType] = mapped_column(Enum(ProductCopyType, name="product_copy_manual_revision_type", native_enum=False, create_constraint=True, values_callable=lambda members: [member.value for member in members]), nullable=False)
    short_description: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)

    product: Mapped[Product] = relationship(back_populates="product_copy_manual_revisions")


class ProductCopyReview(Base):
    __tablename__ = "product_copy_reviews"
    __table_args__ = (
        CheckConstraint(
            "(decision = 'corrected' AND corrected_short_description IS NOT NULL) OR "
            "(decision IN ('approved', 'rejected') "
            "AND corrected_short_description IS NULL)",
            name="ck_product_copy_reviews_corrected_text",
        ),
        CheckConstraint(
            "corrected_short_description IS NULL OR "
            "(length(trim(corrected_short_description)) > 0 "
            "AND length(corrected_short_description) <= 180)",
            name="ck_product_copy_reviews_corrected_text_length",
        ),
        CheckConstraint(
            "(decision = 'rejected' AND applied_at IS NULL) OR "
            "(decision IN ('approved', 'corrected') AND applied_at IS NOT NULL)",
            name="ck_product_copy_reviews_applied_at",
        ),
        UniqueConstraint(
            "product_copy_run_id", name="uq_product_copy_reviews_run"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_copy_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("product_copy_runs.id"), nullable=False
    )
    decision: Mapped[ProductCopyReviewDecision] = mapped_column(
        Enum(
            ProductCopyReviewDecision,
            name="product_copy_review_decision",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    corrected_short_description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    product_copy_run: Mapped[ProductCopyRun] = relationship(
        back_populates="review"
    )


class ImageEnhancementRun(Base):
    __tablename__ = "image_enhancement_runs"
    __table_args__ = (
        CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_image_enhancement_runs_provider_nonempty",
        ),
        CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_image_enhancement_runs_model_nonempty",
        ),
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_image_enhancement_runs_prompt_version_nonempty",
        ),
        CheckConstraint(
            "length(trim(config_version)) > 0",
            name="ck_image_enhancement_runs_config_version_nonempty",
        ),
        CheckConstraint(
            "length(parameters_hash) = 64 "
            "AND parameters_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_image_enhancement_runs_parameters_hash_format",
        ),
        Index("ix_image_enhancement_runs_source_photo_id", "source_photo_id"),
        Index("ix_image_enhancement_runs_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_photo_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("photos.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id")
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    config_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ExtractionRunStatus] = mapped_column(
        Enum(
            ExtractionRunStatus,
            name="image_enhancement_run_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ExtractionRunStatus.RUNNING,
    )
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sanitized_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    source_photo: Mapped[Photo] = relationship(
        back_populates="image_enhancement_runs"
    )
    job: Mapped[Job | None] = relationship(back_populates="image_enhancement_runs")
    derived_image: Mapped["DerivedImage | None"] = relationship(
        back_populates="enhancement_run",
        uselist=False,
    )


class DerivedImage(Base):
    __tablename__ = "derived_images"
    __table_args__ = (
        CheckConstraint(
            "length(trim(file_path)) > 0",
            name="ck_derived_images_file_path_nonempty",
        ),
        CheckConstraint(
            "length(checksum_sha256) = 64 "
            "AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_derived_images_checksum_sha256_format",
        ),
        CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_derived_images_supported_mime_type",
        ),
        CheckConstraint(
            "file_size_bytes > 0",
            name="ck_derived_images_file_size_positive",
        ),
        CheckConstraint("width > 0", name="ck_derived_images_width_positive"),
        CheckConstraint("height > 0", name="ck_derived_images_height_positive"),
        UniqueConstraint(
            "enhancement_run_id",
            name="uq_derived_images_enhancement_run",
        ),
        Index("ix_derived_images_source_photo_id", "source_photo_id"),
        Index("ix_derived_images_checksum_sha256", "checksum_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_photo_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("photos.id"), nullable=False
    )
    enhancement_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("image_enhancement_runs.id"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    width: Mapped[int] = mapped_column(nullable=False)
    height: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    source_photo: Mapped[Photo] = relationship(back_populates="derived_images")
    enhancement_run: Mapped[ImageEnhancementRun] = relationship(
        back_populates="derived_image"
    )
    reviews: Mapped[list["DerivedImageReview"]] = relationship(
        back_populates="derived_image"
    )
    selected_by_preferences: Mapped[list["PhotoPresentationPreference"]] = (
        relationship(back_populates="selected_derived_image")
    )


class DerivedImageReview(Base):
    __tablename__ = "derived_image_reviews"
    __table_args__ = (
        Index(
            "ix_derived_image_reviews_current",
            "derived_image_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    derived_image_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_images.id"), nullable=False
    )
    decision: Mapped[DerivedImageReviewDecision] = mapped_column(
        Enum(
            DerivedImageReviewDecision,
            name="derived_image_review_decision",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    derived_image: Mapped[DerivedImage] = relationship(back_populates="reviews")


class PhotoPresentationPreference(Base):
    __tablename__ = "photo_presentation_preferences"
    __table_args__ = (
        UniqueConstraint(
            "photo_id",
            name="uq_photo_presentation_preferences_photo",
        ),
        Index(
            "ix_photo_presentation_preferences_selected_derived_image_id",
            "selected_derived_image_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    photo_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("photos.id"), nullable=False
    )
    selected_derived_image_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_images.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    photo: Mapped[Photo] = relationship(back_populates="presentation_preference")
    selected_derived_image: Mapped[DerivedImage | None] = relationship(
        back_populates="selected_by_preferences"
    )


class CatalogSnapshot(Base):
    __tablename__ = "catalog_snapshots"
    __table_args__ = (
        CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_catalog_snapshots_schema_version_nonempty",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency) "
            "AND currency NOT GLOB '*[^A-Z]*'",
            name="ck_catalog_snapshots_currency_iso_code",
        ),
        CheckConstraint(
            "length(content_hash) = 64 "
            "AND content_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_snapshots_content_hash_format",
        ),
        Index("ix_catalog_snapshots_content_hash", "content_hash"),
        Index("ix_catalog_snapshots_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    render_runs: Mapped[list["CatalogRenderRun"]] = relationship(
        back_populates="catalog_snapshot"
    )
    artifacts: Mapped[list["CatalogArtifact"]] = relationship(
        back_populates="catalog_snapshot"
    )
    catalog_build: Mapped["CatalogBuild | None"] = relationship(
        back_populates="catalog_snapshot", uselist=False
    )


class CatalogBrandAsset(Base):
    __tablename__ = "catalog_brand_assets"
    __table_args__ = (
        CheckConstraint("length(trim(file_path)) > 0", name="ck_catalog_brand_assets_path"),
        CheckConstraint("length(checksum_sha256) = 64 AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'", name="ck_catalog_brand_assets_checksum"),
        CheckConstraint("mime_type IN ('image/png', 'image/jpeg', 'image/webp')", name="ck_catalog_brand_assets_mime"),
        CheckConstraint("file_size_bytes > 0 AND width > 0 AND height > 0", name="ck_catalog_brand_assets_dimensions"),
        Index("ix_catalog_brand_assets_checksum", "checksum_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    width: Mapped[int] = mapped_column(nullable=False)
    height: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)

    profiles: Mapped[list["CatalogBrandProfile"]] = relationship(back_populates="logo_asset")


class CatalogCoverAsset(Base):
    """Immutable, presentation-only upload; not owned by a Brand profile."""

    __tablename__ = "catalog_cover_assets"
    __table_args__ = (
        CheckConstraint("length(trim(file_path)) > 0", name="ck_catalog_cover_assets_path"),
        CheckConstraint("length(checksum_sha256) = 64 AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'", name="ck_catalog_cover_assets_checksum"),
        CheckConstraint("mime_type IN ('image/png', 'image/jpeg', 'image/webp')", name="ck_catalog_cover_assets_mime"),
        CheckConstraint("file_size_bytes > 0 AND width > 0 AND height > 0", name="ck_catalog_cover_assets_dimensions"),
        Index("ix_catalog_cover_assets_checksum", "checksum_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    width: Mapped[int] = mapped_column(nullable=False)
    height: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class CatalogBrandProfile(Base):
    __tablename__ = "catalog_brand_profiles"
    __table_args__ = (
        CheckConstraint("length(trim(key)) > 0", name="ck_catalog_brand_profiles_key"),
        CheckConstraint("key GLOB '[a-z]*' AND key NOT GLOB '*[^a-z0-9-]*' AND key NOT GLOB '*--*' AND key NOT GLOB '*-'", name="ck_catalog_brand_profiles_key_slug"),
        CheckConstraint("length(trim(display_name)) > 0", name="ck_catalog_brand_profiles_name"),
        CheckConstraint("length(primary_color) = 7 AND primary_color GLOB '#[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'", name="ck_catalog_brand_profiles_primary_color"),
        CheckConstraint("length(accent_color) = 7 AND accent_color GLOB '#[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'", name="ck_catalog_brand_profiles_accent_color"),
        UniqueConstraint("key", name="uq_catalog_brand_profiles_key"),
        Index("ix_catalog_brand_profiles_logo_asset_id", "logo_asset_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    logo_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("catalog_brand_assets.id"))
    primary_color: Mapped[str] = mapped_column(String(7), nullable=False)
    accent_color: Mapped[str] = mapped_column(String(7), nullable=False)
    contact_text: Mapped[str | None] = mapped_column(String(500))
    social_handle: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now)

    logo_asset: Mapped[CatalogBrandAsset | None] = relationship(back_populates="profiles")
    render_runs: Mapped[list["CatalogRenderRun"]] = relationship(back_populates="catalog_brand_profile")


class CatalogRenderRun(Base):
    __tablename__ = "catalog_render_runs"
    __table_args__ = (
        CheckConstraint(
            "length(trim(template_key)) > 0",
            name="ck_catalog_render_runs_template_key_nonempty",
        ),
        CheckConstraint(
            "length(trim(template_version)) > 0",
            name="ck_catalog_render_runs_template_version_nonempty",
        ),
        CheckConstraint(
            "length(template_hash) = 64 "
            "AND template_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_render_runs_template_hash_format",
        ),
        CheckConstraint(
            "length(trim(renderer_version)) > 0",
            name="ck_catalog_render_runs_renderer_version_nonempty",
        ),
        CheckConstraint(
            "length(trim(renderer_engine)) > 0",
            name="ck_catalog_render_runs_renderer_engine_nonempty",
        ),
        CheckConstraint(
            "renderer_engine_version IS NULL "
            "OR length(trim(renderer_engine_version)) > 0",
            name="ck_catalog_render_runs_engine_version_nonempty",
        ),
        CheckConstraint(
            "length(trim(locale)) > 0",
            name="ck_catalog_render_runs_locale_nonempty",
        ),
        CheckConstraint(
            "length(config_hash) = 64 "
            "AND config_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_render_runs_config_hash_format",
        ),
        Index("ix_catalog_render_runs_catalog_snapshot_id", "catalog_snapshot_id"),
        Index("ix_catalog_render_runs_job_id", "job_id"),
        Index("ix_catalog_render_runs_brand_profile_id", "catalog_brand_profile_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    catalog_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_snapshots.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id")
    )
    template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    template_version: Mapped[str] = mapped_column(String(100), nullable=False)
    template_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    renderer_engine: Mapped[str] = mapped_column(String(100), nullable=False)
    renderer_engine_version: Mapped[str | None] = mapped_column(String(255))
    locale: Mapped[str] = mapped_column(String(35), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    layout_key: Mapped[str | None] = mapped_column(String(50))
    layout_version: Mapped[str | None] = mapped_column(String(100))
    catalog_brand_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("catalog_brand_profiles.id"))
    branding_schema_version: Mapped[str | None] = mapped_column(String(100))
    branding_hash: Mapped[str | None] = mapped_column(String(64))
    branding_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    theme_schema_version: Mapped[str | None] = mapped_column(String(100))
    theme_hash: Mapped[str | None] = mapped_column(String(64))
    theme_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cover_schema_version: Mapped[str | None] = mapped_column(String(100))
    cover_hash: Mapped[str | None] = mapped_column(String(64))
    cover_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[ExtractionRunStatus] = mapped_column(
        Enum(
            ExtractionRunStatus,
            name="catalog_render_run_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ExtractionRunStatus.RUNNING,
    )
    sanitized_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    catalog_snapshot: Mapped[CatalogSnapshot] = relationship(
        back_populates="render_runs"
    )
    job: Mapped[Job | None] = relationship(back_populates="catalog_render_runs")
    catalog_brand_profile: Mapped[CatalogBrandProfile | None] = relationship(back_populates="render_runs")
    artifact: Mapped["CatalogArtifact | None"] = relationship(
        back_populates="render_run", uselist=False
    )


class CatalogArtifact(Base):
    __tablename__ = "catalog_artifacts"
    __table_args__ = (
        CheckConstraint(
            "media_type = 'application/pdf'",
            name="ck_catalog_artifacts_pdf_media_type",
        ),
        CheckConstraint(
            "length(trim(file_path)) > 0",
            name="ck_catalog_artifacts_file_path_nonempty",
        ),
        CheckConstraint(
            "length(checksum_sha256) = 64 "
            "AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_artifacts_checksum_sha256_format",
        ),
        CheckConstraint(
            "file_size_bytes > 0",
            name="ck_catalog_artifacts_file_size_positive",
        ),
        CheckConstraint(
            "page_count > 0",
            name="ck_catalog_artifacts_page_count_positive",
        ),
        UniqueConstraint(
            "render_run_id",
            name="uq_catalog_artifacts_render_run",
        ),
        Index("ix_catalog_artifacts_catalog_snapshot_id", "catalog_snapshot_id"),
        Index("ix_catalog_artifacts_checksum_sha256", "checksum_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    catalog_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_snapshots.id"), nullable=False
    )
    render_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_render_runs.id"), nullable=False
    )
    media_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="application/pdf"
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    page_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    catalog_snapshot: Mapped[CatalogSnapshot] = relationship(
        back_populates="artifacts"
    )
    render_run: Mapped[CatalogRenderRun] = relationship(back_populates="artifact")


class CatalogBuild(Base):
    """Idempotent ownership for one explicit Catalog Builder create action."""

    __tablename__ = "catalog_builds"
    __table_args__ = (
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_catalog_builds_idempotency_key_nonempty",
        ),
        CheckConstraint(
            "length(request_hash) = 64 "
            "AND request_hash NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_catalog_builds_request_hash_format",
        ),
        UniqueConstraint("idempotency_key", name="uq_catalog_builds_idempotency_key"),
        UniqueConstraint("catalog_snapshot_id", name="uq_catalog_builds_snapshot"),
        UniqueConstraint("job_id", name="uq_catalog_builds_job"),
        Index("ix_catalog_builds_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_snapshots.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    catalog_snapshot: Mapped[CatalogSnapshot] = relationship(
        back_populates="catalog_build"
    )
    job: Mapped[Job] = relationship(back_populates="catalog_build")
