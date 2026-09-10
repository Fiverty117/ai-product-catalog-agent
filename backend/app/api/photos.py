import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.enums import PhotoRole
from app.domain.schemas import PhotoRead
from app.services.photo_intake import (
    DEFAULT_ORIGINALS_DIR,
    PhotoIntakeError,
    UnknownSKUError,
    UnsupportedPhotoFormatError,
    register_original_photo,
)

router = APIRouter(prefix="/photos", tags=["photos"])


def get_originals_dir() -> Path:
    return DEFAULT_ORIGINALS_DIR


@router.post("", response_model=PhotoRead, status_code=status.HTTP_201_CREATED)
async def intake_original_photo(
    image: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db)],
    originals_dir: Annotated[Path, Depends(get_originals_dir)],
    sku_id: Annotated[uuid.UUID | None, Form()] = None,
    role: Annotated[PhotoRole, Form()] = PhotoRole.OTHER,
) -> PhotoRead:
    image_bytes = await image.read()
    try:
        photo = register_original_photo(
            session,
            image_bytes=image_bytes,
            original_filename=image.filename or "",
            originals_dir=originals_dir,
            sku_id=sku_id,
            role=role,
        )
        session.commit()
        session.refresh(photo)
    except UnknownSKUError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UnsupportedPhotoFormatError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except PhotoIntakeError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    finally:
        await image.close()

    return PhotoRead.model_validate(photo)
