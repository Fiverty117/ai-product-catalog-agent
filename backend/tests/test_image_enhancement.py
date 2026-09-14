import hashlib
import uuid
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, Brand, DerivedImage, ImageEnhancementRun, Photo, Product, SKU
from app.db.session import create_sqlite_engine
from app.domain.enums import ExtractionRunStatus
from app.domain.schemas import ImageEnhancementJobPayload, ImageEnhancementRunRead
from app.services.image_enhancement import (
    IMAGE_ENHANCEMENT_CONFIG_VERSION,
    CompletedImageEnhancementRunError,
    DerivedImageStorageError,
    IneligibleImageEnhancementPhotoError,
    StoredDerivedImage,
    build_image_enhancement_idempotency_key,
    complete_image_enhancement_run,
    create_running_image_enhancement_run,
    enqueue_image_enhancement,
    mark_image_enhancement_run_failed,
    store_processed_image,
)
from app.services.photo_intake import register_original_photo


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'enhancement.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.info["originals"] = tmp_path / "storage" / "originals"
        session.info["processed"] = tmp_path / "storage" / "processed"
        yield session
    engine.dispose()


def image_bytes(image_format="PNG", size=(12, 8), color=(10, 20, 30)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def original_photo(session: Session) -> Photo:
    sku = SKU(product=Product(name="Product", brand=Brand(name="Brand")))
    session.add(sku)
    session.flush()
    return register_original_photo(
        session,
        image_bytes=image_bytes(),
        original_filename="source.png",
        originals_dir=session.info["originals"],
        sku_id=sku.id,
    )


def payload(photo: Photo, **overrides) -> ImageEnhancementJobPayload:
    values = {
        "source_photo_id": photo.id,
        "source_checksum_sha256": photo.checksum_sha256,
        "provider": "openai",
        "model": "gpt-image-2.5-sunburst",
        "prompt_version": "product-image-enhancement-v1",
        "config_version": IMAGE_ENHANCEMENT_CONFIG_VERSION,
        "parameters": {
            "quality": "high",
            "output_format": "png",
            "size": "auto",
            "background": "auto",
        },
        **overrides,
    }
    return ImageEnhancementJobPayload.model_validate(values)


def stored() -> StoredDerivedImage:
    return StoredDerivedImage(
        file_path="storage/processed/aa/output.png",
        checksum_sha256="a" * 64,
        mime_type="image/png",
        file_size_bytes=100,
        width=12,
        height=8,
    )


def test_enqueue_original_is_idempotent_and_does_not_commit(session: Session) -> None:
    photo = original_photo(session)
    first = enqueue_image_enhancement(session, source_photo_id=photo.id)
    second = enqueue_image_enhancement(session, source_photo_id=photo.id)

    assert first is second
    assert first.payload["source_photo_id"] == str(photo.id)
    assert "file_path" not in first.payload
    assert first.payload["parameters"]["quality"] == "high"


@pytest.mark.parametrize(
    "override",
    [
        {"model": "different-model"},
        {"prompt_version": "product-image-enhancement-v2"},
        {"config_version": "image-enhancement-config-v2"},
        {"source_checksum_sha256": "b" * 64},
        {
            "parameters": {
                "quality": "medium",
                "output_format": "png",
                "size": "auto",
                "background": "auto",
            }
        },
    ],
)
def test_source_model_prompt_and_config_change_logical_key(
    session: Session, override: dict
) -> None:
    photo = original_photo(session)
    baseline = payload(photo)

    assert build_image_enhancement_idempotency_key(
        baseline
    ) != build_image_enhancement_idempotency_key(payload(photo, **override))


def test_non_original_source_is_rejected(session: Session) -> None:
    photo = Photo(
        file_path="derived.png",
        checksum_sha256="a" * 64,
        role="front",
        is_original=False,
    )
    session.add(photo)
    session.flush()

    with pytest.raises(IneligibleImageEnhancementPhotoError, match="original"):
        enqueue_image_enhancement(session, source_photo_id=photo.id)


def test_identical_bytes_on_distinct_photo_records_keep_exact_lineage(
    session: Session,
) -> None:
    first = original_photo(session)
    second = register_original_photo(
        session,
        image_bytes=Path(first.file_path).read_bytes(),
        original_filename="same-bytes.png",
        originals_dir=session.info["originals"],
        sku_id=first.sku_id,
    )

    assert first.checksum_sha256 == second.checksum_sha256
    assert build_image_enhancement_idempotency_key(
        payload(first)
    ) != build_image_enhancement_idempotency_key(payload(second))


def test_run_success_creates_exact_immutable_lineage(session: Session) -> None:
    photo = original_photo(session)
    run = create_running_image_enhancement_run(session, payload=payload(photo))
    derived = complete_image_enhancement_run(
        session,
        run,
        stored_image=stored(),
        usage={"input_tokens": 10},
    )

    assert run.status is ExtractionRunStatus.SUCCEEDED
    assert run.completed_at.utcoffset() == timedelta(0)
    assert derived.source_photo_id == photo.id
    assert derived.enhancement_run_id == run.id
    assert not hasattr(derived, "product_id")
    assert not hasattr(derived, "sku_id")
    assert ImageEnhancementRunRead.model_validate(run).parameters_hash
    with pytest.raises(CompletedImageEnhancementRunError):
        mark_image_enhancement_run_failed(session, run, error="late failure")


def test_failed_run_is_sanitized_and_terminal(session: Session) -> None:
    photo = original_photo(session)
    run = create_running_image_enhancement_run(session, payload=payload(photo))
    mark_image_enhancement_run_failed(
        session,
        run,
        error="api_key=sk-sensitive local C:/secret/source.png",
    )

    assert run.status is ExtractionRunStatus.FAILED
    assert "sk-sensitive" not in run.sanitized_error
    with pytest.raises(CompletedImageEnhancementRunError):
        complete_image_enhancement_run(session, run, stored_image=stored())


@pytest.mark.parametrize(
    ("image_format", "mime_type", "extension"),
    [("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg"), ("WEBP", "image/webp", ".webp")],
)
def test_processed_storage_detects_content_and_uses_exact_checksum(
    session: Session, image_format: str, mime_type: str, extension: str
) -> None:
    content = image_bytes(image_format=image_format, size=(17, 13))
    stored_image = store_processed_image(
        content, processed_dir=session.info["processed"]
    )

    assert stored_image.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert stored_image.mime_type == mime_type
    assert (stored_image.width, stored_image.height) == (17, 13)
    path = Path(stored_image.file_path)
    assert path.suffix == extension
    assert path.is_relative_to(session.info["processed"].resolve())
    assert path.read_bytes() == content


def test_identical_output_reuses_physical_file_without_collapsing_audit(
    session: Session,
) -> None:
    content = image_bytes()
    first = store_processed_image(content, processed_dir=session.info["processed"])
    second = store_processed_image(content, processed_dir=session.info["processed"])
    photo = original_photo(session)
    first_run = create_running_image_enhancement_run(session, payload=payload(photo))
    first_derived = complete_image_enhancement_run(
        session, first_run, stored_image=first
    )
    second_run = create_running_image_enhancement_run(session, payload=payload(photo))
    second_derived = complete_image_enhancement_run(
        session, second_run, stored_image=second
    )

    assert first == second
    assert len(list(session.info["processed"].rglob("*.png"))) == 1
    assert first_derived.id != second_derived.id
    assert first_derived.enhancement_run_id != second_derived.enhancement_run_id
    assert first_derived.file_path == second_derived.file_path


@pytest.mark.parametrize("content", [b"", b"not-an-image", image_bytes("GIF")])
def test_corrupt_or_unsupported_output_is_rejected(
    session: Session, content: bytes
) -> None:
    with pytest.raises(DerivedImageStorageError, match="corrupt|unsupported"):
        store_processed_image(content, processed_dir=session.info["processed"])


def test_original_metadata_and_bytes_remain_unchanged(session: Session) -> None:
    photo = original_photo(session)
    original = (
        photo.file_path,
        photo.checksum_sha256,
        photo.width,
        photo.height,
        Path(photo.file_path).read_bytes(),
    )
    run = create_running_image_enhancement_run(session, payload=payload(photo))
    output = store_processed_image(
        image_bytes(color=(90, 80, 70)), processed_dir=session.info["processed"]
    )
    complete_image_enhancement_run(session, run, stored_image=output)

    assert (
        photo.file_path,
        photo.checksum_sha256,
        photo.width,
        photo.height,
        Path(photo.file_path).read_bytes(),
    ) == original
    assert session.scalar(select(DerivedImage)).source_photo_id == photo.id
