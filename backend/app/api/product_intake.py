import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.photos import get_originals_dir
from app.db.models import ProductIntakeItem
from app.db.session import get_db
from app.domain.product_intake import (
    PrimaryPhotoRequest, ProductIntakeDraft, ProductIntakeList, ProductIntakeRead,
    ProductIntakePromotionContext, ProductIntakePromotionRequest, ProductIntakePromotionResult,
)
from app.services.photo_intake import PhotoIntakeError
from app.services.photo_ownership import PhotoOwnershipError
from app.services.product_intake import (
    IntakeAssetError, IntakeConflictError, IntakeInputError, UnknownIntakeError,
    add_photo, create_intake, enqueue_extraction, intake_read, require_intake,
    resolve_intake_photo, save_draft, set_primary_photo,
)
from app.services.product_intake_promotion import (
    PromotionConflictError, PromotionInputError, PromotionResourceError,
    promote_intake, promotion_context,
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


def _lock_error(session: Session, exc: OperationalError) -> HTTPException:
    session.rollback()
    if "database is locked" in str(exc).lower():
        return HTTPException(409, "Another Intake operation is in progress. Retry this action.")
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
    except (UnknownIntakeError, IntakeConflictError) as exc:
        raise _error(session, exc) from exc


@router.post("/{intake_id}/photos", response_model=ProductIntakeRead)
async def upload_photos(intake_id: uuid.UUID, session: DB, originals_dir: Originals, images: Annotated[list[UploadFile], File()]) -> ProductIntakeRead:
    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        item = require_intake(session, intake_id)
        await _upload_files(session, item, images, originals_dir)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError, IntakeInputError, PhotoIntakeError) as exc:
        raise _error(session, exc) from exc
    except OperationalError as exc:
        raise _lock_error(session, exc) from exc


@router.put("/{intake_id}/primary-photo", response_model=ProductIntakeRead)
def primary_photo(intake_id: uuid.UUID, request: PrimaryPhotoRequest, session: DB) -> ProductIntakeRead:
    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        item = require_intake(session, intake_id)
        set_primary_photo(session, item, request.photo_id)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError) as exc:
        raise _error(session, exc) from exc
    except OperationalError as exc:
        raise _lock_error(session, exc) from exc


@router.put("/{intake_id}/draft", response_model=ProductIntakeRead)
def update_draft(intake_id: uuid.UUID, draft: ProductIntakeDraft, session: DB) -> ProductIntakeRead:
    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        item = require_intake(session, intake_id)
        save_draft(session, item, draft)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError) as exc:
        raise _error(session, exc) from exc
    except OperationalError as exc:
        raise _lock_error(session, exc) from exc


@router.post("/{intake_id}/extractions", response_model=ProductIntakeRead, status_code=status.HTTP_202_ACCEPTED)
def run_extraction(intake_id: uuid.UUID, session: DB, idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")]) -> ProductIntakeRead:
    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        item = require_intake(session, intake_id)
        enqueue_extraction(session, item, idempotency_key)
        result = intake_read(session, item)
        session.commit()
        return result
    except (UnknownIntakeError, IntakeConflictError, IntakeInputError) as exc:
        raise _error(session, exc) from exc
    except OperationalError as exc:
        raise _lock_error(session, exc) from exc


@router.get("/{intake_id}/photos/{photo_id}/image", response_class=FileResponse)
def photo_image(intake_id: uuid.UUID, photo_id: uuid.UUID, session: DB, originals_dir: Originals) -> FileResponse:
    try:
        item = require_intake(session, intake_id)
        path, mime = resolve_intake_photo(session, item, photo_id, originals_dir)
        return FileResponse(path, media_type=mime)
    except (UnknownIntakeError, IntakeConflictError, IntakeAssetError) as exc:
        raise _error(session, exc) from exc


@router.get("/{intake_id}/promotion", response_model=ProductIntakePromotionContext)
def get_promotion(intake_id: uuid.UUID, session: DB) -> ProductIntakePromotionContext:
    try:
        return promotion_context(session, intake_id)
    except UnknownIntakeError as exc:
        raise _error(session, exc) from exc


@router.post("/{intake_id}/promotion", response_model=ProductIntakePromotionResult, status_code=status.HTTP_201_CREATED)
def create_promotion(intake_id: uuid.UUID, request: ProductIntakePromotionRequest, session: DB, originals_dir: Originals) -> ProductIntakePromotionResult:
    try:
        result = promote_intake(session, intake_id, request, originals_dir=originals_dir)
        session.commit()
        return result
    except UnknownIntakeError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except PromotionResourceError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except IntakeAssetError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except (PromotionConflictError, IntakeConflictError, PhotoOwnershipError) as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (PromotionInputError, PhotoIntakeError, ValueError) as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Promotion conflicted with a concurrent canonical write.") from exc
    except OperationalError as exc:
        session.rollback()
        if "database is locked" in str(exc).lower():
            raise HTTPException(409, "Another promotion is in progress. Retry this action.") from exc
        raise HTTPException(500, "Product promotion failed.") from exc
    except SQLAlchemyError as exc:
        session.rollback()
        raise HTTPException(500, "Product promotion failed.") from exc
