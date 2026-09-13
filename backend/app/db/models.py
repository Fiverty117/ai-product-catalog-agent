import uuid
from datetime import datetime
from decimal import Decimal

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime, utc_now
from app.domain.enums import (
    ExtractionRunStatus,
    FieldSource,
    FieldState,
    JobStatus,
    PhotoRole,
    SKUFieldName,
)


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
    __table_args__ = (CheckConstraint("length(trim(name)) > 0", name="ck_brands_name_nonempty"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    products: Mapped[list["Product"]] = relationship(back_populates="brand")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_products_name_nonempty"),
        Index("ix_products_brand_id", "brand_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("brands.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    brand: Mapped[Brand] = relationship(back_populates="products")
    skus: Mapped[list["SKU"]] = relationship(back_populates="product")


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
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sku_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id"), nullable=False
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
        Index("ix_photos_sku_id", "sku_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id"), nullable=True
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
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(
        secondary=extraction_run_photos,
        back_populates="photos",
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
