import hashlib
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.photos import get_originals_dir
from app.db import Base, Brand, Photo, Product, SKU
from app.db.session import create_sqlite_engine, get_db
from app.main import app


def make_image_bytes(image_format: str, *, size: tuple[int, int] = (13, 17)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=(24, 96, 160)).save(output, format=image_format)
    return output.getvalue()


@pytest.fixture
def intake_client(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'photo-intake.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    originals_dir = tmp_path / "assets" / "originals"

    def override_get_db():
        with session_factory() as session:
            yield session

    def override_originals_dir() -> Path:
        return originals_dir

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_originals_dir] = override_originals_dir
    with TestClient(app) as client:
        yield client, session_factory, originals_dir
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize(
    ("image_format", "filename", "expected_mime", "expected_suffix"),
    [
        ("JPEG", "product.jpeg", "image/jpeg", ".jpg"),
        ("PNG", "product.png", "image/png", ".png"),
        ("WEBP", "product.webp", "image/webp", ".webp"),
    ],
)
def test_valid_original_photo_intake_preserves_bytes_and_metadata(
    intake_client,
    image_format: str,
    filename: str,
    expected_mime: str,
    expected_suffix: str,
) -> None:
    client, session_factory, originals_dir = intake_client
    original_bytes = make_image_bytes(image_format)

    response = client.post(
        "/photos",
        files={"image": (filename, original_bytes, "application/octet-stream")},
        data={"role": "front"},
    )

    assert response.status_code == 201
    payload = response.json()
    expected_checksum = hashlib.sha256(original_bytes).hexdigest()
    assert payload["sku_id"] is None
    assert payload["product_id"] is None
    assert payload["checksum_sha256"] == expected_checksum
    assert payload["original_filename"] == filename
    assert payload["mime_type"] == expected_mime
    assert payload["file_size_bytes"] == len(original_bytes)
    assert (payload["width"], payload["height"]) == (13, 17)
    assert payload["role"] == "front"
    assert payload["is_original"] is True

    stored_path = Path(payload["file_path"])
    assert stored_path.is_relative_to(originals_dir.resolve())
    assert stored_path.suffix == expected_suffix
    assert stored_path.read_bytes() == original_bytes

    with session_factory() as session:
        photo = session.get(Photo, uuid.UUID(payload["id"]))
        assert photo is not None
        assert photo.sku_id is None
        assert photo.product_id is None


def test_actual_content_determines_format_not_filename(intake_client) -> None:
    client, _, _ = intake_client
    png_bytes = make_image_bytes("PNG")

    response = client.post(
        "/photos",
        files={"image": ("misleading.jpg", png_bytes, "image/jpeg")},
    )

    assert response.status_code == 201
    assert response.json()["mime_type"] == "image/png"
    assert Path(response.json()["file_path"]).suffix == ".png"


def test_photo_intake_can_link_existing_sku(intake_client) -> None:
    client, session_factory, _ = intake_client
    with session_factory() as session:
        sku = SKU(product=Product(name="Whey", brand=Brand(name="Test Brand")))
        session.add(sku)
        session.commit()
        sku_id = sku.id

    response = client.post(
        "/photos",
        files={"image": ("back.png", make_image_bytes("PNG"), "image/png")},
        data={"sku_id": str(sku_id), "role": "back"},
    )

    assert response.status_code == 201
    assert response.json()["sku_id"] == str(sku_id)
    assert response.json()["role"] == "back"
    with session_factory() as session:
        photo = session.get(Photo, uuid.UUID(response.json()["id"]))
        assert photo is not None
        assert photo.sku_id == sku_id


def test_unknown_sku_is_rejected_before_file_storage(intake_client) -> None:
    client, session_factory, originals_dir = intake_client

    response = client.post(
        "/photos",
        files={"image": ("front.jpg", make_image_bytes("JPEG"), "image/jpeg")},
        data={"sku_id": str(uuid.uuid4())},
    )

    assert response.status_code == 404
    assert not originals_dir.exists()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 0


@pytest.mark.parametrize("invalid_bytes", [b"", b"not an image"])
def test_empty_or_corrupt_image_is_rejected(
    intake_client, invalid_bytes: bytes
) -> None:
    client, session_factory, originals_dir = intake_client

    response = client.post(
        "/photos",
        files={"image": ("broken.jpg", invalid_bytes, "image/jpeg")},
    )

    assert response.status_code == 422
    assert not originals_dir.exists()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 0


def test_decodable_unsupported_image_format_is_rejected(intake_client) -> None:
    client, session_factory, originals_dir = intake_client

    response = client.post(
        "/photos",
        files={"image": ("product.gif", make_image_bytes("GIF"), "image/gif")},
    )

    assert response.status_code == 415
    assert not originals_dir.exists()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 0


def test_identical_upload_reuses_one_physical_asset(intake_client) -> None:
    client, session_factory, originals_dir = intake_client
    original_bytes = make_image_bytes("WEBP")

    first = client.post(
        "/photos",
        files={"image": ("first.webp", original_bytes, "image/webp")},
    )
    second = client.post(
        "/photos",
        files={"image": ("renamed.webp", original_bytes, "image/webp")},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["file_path"] == second.json()["file_path"]
    assert first.json()["id"] != second.json()["id"]
    assert [path for path in originals_dir.rglob("*") if path.is_file()] == [
        Path(first.json()["file_path"])
    ]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 2
