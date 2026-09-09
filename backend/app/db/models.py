import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, Index, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime, utc_now
from app.domain.enums import PhotoRole


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


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (
        CheckConstraint("length(trim(file_path)) > 0", name="ck_photos_file_path_nonempty"),
        CheckConstraint(
            "length(checksum_sha256) = 64 "
            "AND checksum_sha256 NOT GLOB '*[^0-9a-fA-F]*'",
            name="ck_photos_checksum_sha256_format",
        ),
        Index("ix_photos_sku_id", "sku_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skus.id"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
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

    sku: Mapped[SKU] = relationship(back_populates="photos")


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
