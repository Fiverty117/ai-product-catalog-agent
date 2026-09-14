import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Photo, Product, SKU
from app.domain.enums import PhotoRole


class PhotoOwnershipError(ValueError):
    pass


class UnknownPhotoError(PhotoOwnershipError):
    pass


class UnknownPhotoProductError(PhotoOwnershipError):
    pass


class UnknownPhotoSKUError(PhotoOwnershipError):
    pass


class PhotoAlreadyOwnedError(PhotoOwnershipError):
    pass


def assign_photo_to_product(
    session: Session,
    *,
    photo_id: uuid.UUID,
    product_id: uuid.UUID,
    replace_owner: bool = False,
) -> Photo:
    """Assign a Photo to a Product as an explicit, caller-owned human action."""

    photo = _require_photo(session, photo_id)
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownPhotoProductError(f"Product not found: {product_id}")
    if photo.product_id == product.id and photo.sku_id is None:
        return photo
    _require_replaceable(photo, replace_owner=replace_owner)
    photo.sku = None
    photo.product = product
    session.flush()
    return photo


def assign_photo_to_sku(
    session: Session,
    *,
    photo_id: uuid.UUID,
    sku_id: uuid.UUID,
    replace_owner: bool = False,
) -> Photo:
    """Assign a Photo to an SKU as an explicit, caller-owned human action."""

    photo = _require_photo(session, photo_id)
    sku = session.get(SKU, sku_id)
    if sku is None:
        raise UnknownPhotoSKUError(f"SKU not found: {sku_id}")
    if photo.sku_id == sku.id and photo.product_id is None:
        return photo
    _require_replaceable(photo, replace_owner=replace_owner)
    photo.product = None
    photo.sku = sku
    session.flush()
    return photo


def get_effective_photos_for_sku(
    session: Session,
    sku_id: uuid.UUID,
    role: PhotoRole | str | None = None,
) -> list[Photo]:
    """Use role-matched SKU photos, otherwise role-matched Product photos."""

    sku = session.get(SKU, sku_id)
    if sku is None:
        raise UnknownPhotoSKUError(f"SKU not found: {sku_id}")
    resolved_role = PhotoRole(role) if role is not None else None

    sku_query = select(Photo).where(Photo.sku_id == sku.id)
    if resolved_role is not None:
        sku_query = sku_query.where(Photo.role == resolved_role)
    sku_photos = session.scalars(
        sku_query.order_by(Photo.created_at, Photo.id)
    ).all()
    if sku_photos:
        return list(sku_photos)

    product_query = select(Photo).where(Photo.product_id == sku.product_id)
    if resolved_role is not None:
        product_query = product_query.where(Photo.role == resolved_role)
    return list(
        session.scalars(product_query.order_by(Photo.created_at, Photo.id)).all()
    )


def _require_photo(session: Session, photo_id: uuid.UUID) -> Photo:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise UnknownPhotoError(f"Photo not found: {photo_id}")
    if photo.product_id is not None and photo.sku_id is not None:
        raise PhotoOwnershipError("Photo cannot belong to both a Product and an SKU")
    return photo


def _require_replaceable(photo: Photo, *, replace_owner: bool) -> None:
    if (photo.product_id is not None or photo.sku_id is not None) and not replace_owner:
        raise PhotoAlreadyOwnedError(
            "Photo already has an owner; set replace_owner=true for an explicit move"
        )
