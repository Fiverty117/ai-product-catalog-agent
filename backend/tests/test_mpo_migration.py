from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Brand, Photo, Product, SKU, PhotoPresentationPreference
from app.db.models import ProductIntakeItem, ProductIntakePhoto


def test_mpo_migration_preserves_graph_and_has_safe_downgrade(tmp_path):
    config = Config("alembic.ini")
    url = f"sqlite:///{tmp_path / 'mpo-migration.db'}"
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "20261004_0027")
    engine = create_engine(url)
    at = datetime(2026, 9, 25, tzinfo=timezone.utc)
    with Session(engine) as session:
        product = Product(name="Existing Product", brand=Brand(name="Existing Brand"))
        sku = SKU(product=product, flavor="Plain")
        item = ProductIntakeItem(status="draft", draft={})
        session.add_all([product, sku, item])
        session.flush()
        for index, mime in enumerate(("image/jpeg", "image/png", "image/webp")):
            photo = Photo(
                product=product if index == 0 else None,
                sku=sku if index == 1 else None,
                file_path=f"storage/originals/{index}.jpg", checksum_sha256=str(index) * 64,
                original_filename=f"original-{index}", mime_type=mime,
                file_size_bytes=123, width=18, height=12, is_original=True,
                created_at=at,
            )
            session.add(photo)
            session.flush()
            session.add(PhotoPresentationPreference(photo=photo))
            if index == 2:
                session.add(ProductIntakePhoto(intake_item_id=item.id, photo=photo, position=0, is_primary=True))
        session.commit()
    with engine.connect() as connection:
        before = connection.execute(text("SELECT * FROM photos ORDER BY id")).mappings().all()
        links = connection.execute(text("SELECT * FROM product_intake_photos")).mappings().all()
        preferences = connection.execute(text("SELECT * FROM photo_presentation_preferences ORDER BY id")).mappings().all()
    inspector = inspect(engine)
    indexes = inspector.get_indexes("photos")
    foreign_keys = inspector.get_foreign_keys("photos")
    checks = {row["name"]: row["sqltext"] for row in inspector.get_check_constraints("photos")}
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert inspector.get_indexes("photos") == indexes
    assert inspector.get_foreign_keys("photos") == foreign_keys
    after_checks = {row["name"]: row["sqltext"] for row in inspector.get_check_constraints("photos")}
    assert "image/mpo" in after_checks["ck_photos_supported_mime_type"]
    assert {k: v for k, v in checks.items() if k != "ck_photos_supported_mime_type"} == {
        k: v for k, v in after_checks.items() if k != "ck_photos_supported_mime_type"
    }
    with engine.begin() as connection:
        assert connection.execute(text("SELECT * FROM photos ORDER BY id")).mappings().all() == before
        assert connection.execute(text("SELECT * FROM product_intake_photos")).mappings().all() == links
        assert connection.execute(text("SELECT * FROM photo_presentation_preferences ORDER BY id")).mappings().all() == preferences
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        connection.execute(text(
            "INSERT INTO photos (id, file_path, checksum_sha256, original_filename, mime_type, "
            "file_size_bytes, width, height, role, is_original, created_at) "
            "VALUES (:id, 'storage/originals/source.mpo', :hash, 'phone.JPEG', 'image/mpo', 100, 18, 12, 'other', 1, :at)"
        ), {"id": "a" * 32, "hash": "b" * 64, "at": "2026-09-25 00:00:00.000000"})
        with pytest.raises(IntegrityError, match="ck_photos_supported_mime_type"):
            connection.execute(text("UPDATE photos SET is_original = 0 WHERE mime_type = 'image/mpo'"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="Cannot downgrade.*MPO Photos exist"):
        command.downgrade(config, "20261004_0027")
    engine = create_engine(url)
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM photos WHERE mime_type='image/mpo'")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261005_0028"
        connection.execute(text("DELETE FROM photos WHERE mime_type='image/mpo'"))  # Test DB only.
    engine.dispose()
    command.downgrade(config, "20261004_0027")
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM photos ORDER BY id")).mappings().all() == before
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()
