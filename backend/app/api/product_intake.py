import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.photos import get_originals_dir
from app.db.models import ProductIntakeItem
from app.db.session import get_db
from app.domain.product_intake import PrimaryPhotoRequest, ProductIntakeDraft, ProductIntakeList, ProductIntakeRead
from app.services.photo_intake import PhotoIntakeError
from app.services.product_intake import (
    IntakeAssetError, IntakeConflictError, IntakeInputError, UnknownIntakeError,
    add_photo, create_intake, enqueue_extraction, intake_read, require_intake,
    resolve_intake_photo, save_draft, set_primary_photo,
)

router = APIRouter(prefix="/api/product-intake/items", tags=["product-intake"])
DB = Annotated[Session, Depends(get_db)]
Originals = Annotated[Path, Depends(get_originals_dir)]


def _error(session: Session, exc: Exception) -> HTTPException:
    session.rollback()
    if isinstance(exc, UnknownIntakeError):
        return HTTPException(404, str(exc))
    if isinstance(exc, IntakeConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, (IntakeInputError, PhotoIntakeError)):
        return HTTPException(422, str(exc))
    if isinstance(exc, IntakeAssetError):
        return HTTPException(404, str(exc))
    return HTTPException(500, "Product intake operation failed.")


async def _upload_files(session: Session, item: ProductIntakeItem, images: list[UploadFile], originals_dir: Path) -> None:
    if len(item.photos) + len(images) > 12:
        raise IntakeInputError("An intake item supports at most 12 photos.")
    for image in images:
        try:
            content = await image.read(20 * 1024 * 1024 + 1)
            add_photo(session, item, image_bytes=content, filename=image.filename or "", originals_dir=originals_dir)
        finally:
            await image.close()


@router.get("", response_model=ProductIntakeList)
def list_items(session: DB) -> ProductIntakeList:
    items = session.scalars(select(ProductIntakeItem).order_by(ProductIntakeItem.updated_at.desc(), ProductIntakeItem.id.desc())).all()
    result = ProductIntakeList(items=[intake_read(session, item) for item in items])
    session.commit()
    return result


@router.post("", response_model=ProductIntakeRead, status_code=status.HTTP_201_CREATED)
async def create_item(session: DB, originals_dir: Originals, images: Annotated[list[UploadFile] | None, File()] = None) -> ProductIntakeRead:
    try:
        item = create_intake(session)
        await _upload_files(session, item, images or [], originals_dir)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError, IntakeInputError, PhotoIntakeError) as exc:
        raise _error(session, exc) from exc


@router.get("/{intake_id}", response_model=ProductIntakeRead)
def get_item(intake_id: uuid.UUID, session: DB) -> ProductIntakeRead:
    try:
        result = intake_read(session, require_intake(session, intake_id))
        session.commit()
        return result
    except UnknownIntakeError as exc:
        raise _error(session, exc) from exc


@router.post("/{intake_id}/photos", response_model=ProductIntakeRead)
async def upload_photos(intake_id: uuid.UUID, session: DB, originals_dir: Originals, images: Annotated[list[UploadFile], File()]) -> ProductIntakeRead:
    try:
        item = require_intake(session, intake_id)
        await _upload_files(session, item, images, originals_dir)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError, IntakeInputError, PhotoIntakeError) as exc:
        raise _error(session, exc) from exc


@router.put("/{intake_id}/primary-photo", response_model=ProductIntakeRead)
def primary_photo(intake_id: uuid.UUID, request: PrimaryPhotoRequest, session: DB) -> ProductIntakeRead:
    try:
        item = require_intake(session, intake_id)
        set_primary_photo(session, item, request.photo_id)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError) as exc:
        raise _error(session, exc) from exc


@router.put("/{intake_id}/draft", response_model=ProductIntakeRead)
def update_draft(intake_id: uuid.UUID, draft: ProductIntakeDraft, session: DB) -> ProductIntakeRead:
    try:
        item = require_intake(session, intake_id)
        save_draft(session, item, draft)
        result = intake_read(session, item)
        session.commit()
        return result
    except UnknownIntakeError as exc:
        raise _error(session, exc) from exc


@router.post("/{intake_id}/extractions", response_model=ProductIntakeRead, status_code=status.HTTP_202_ACCEPTED)
def run_extraction(intake_id: uuid.UUID, session: DB, idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")]) -> ProductIntakeRead:
    try:
        item = require_intake(session, intake_id)
        enqueue_extraction(session, item, idempotency_key)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError, IntakeInputError) as exc:
        raise _error(session, exc) from exc


@router.get("/{intake_id}/photos/{photo_id}/image", response_class=FileResponse)
def photo_image(intake_id: uuid.UUID, photo_id: uuid.UUID, session: DB, originals_dir: Originals) -> FileResponse:
    try:
        item = require_intake(session, intake_id)
        path, mime = resolve_intake_photo(session, item, photo_id, originals_dir)
        return FileResponse(path, media_type=mime)
    except (UnknownIntakeError, IntakeConflictError, IntakeAssetError) as exc:
        raise _error(session, exc) from exc
