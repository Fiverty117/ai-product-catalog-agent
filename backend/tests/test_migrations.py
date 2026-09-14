from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


def test_initial_migration_upgrades_clean_database(tmp_path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version",
        "brands",
        "extraction_field_reviews",
        "extraction_identity_resolutions",
        "extraction_run_photos",
        "extraction_runs",
        "jobs",
        "photos",
        "prices",
        "products",
        "skus",
        "sku_field_provenance",
    }
    engine.dispose()

    command.check(config)


def test_review_migration_preserves_provenance_without_fabricated_lineage(
    tmp_path,
) -> None:
    database_path = tmp_path / "existing-provenance.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260912_0005")

    engine = create_engine(f"sqlite:///{database_path}")
    identifiers = {
        "brand_id": "1" * 32,
        "product_id": "2" * 32,
        "sku_id": "3" * 32,
        "provenance_id": "4" * 32,
    }
    timestamp = "2026-09-13 00:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands (id, name, created_at, updated_at) "
                "VALUES (:brand_id, 'Brand', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products (id, brand_id, name, created_at, updated_at) "
                "VALUES (:product_id, :brand_id, 'Product', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO skus (id, product_id, flavor, created_at, updated_at) "
                "VALUES (:sku_id, :product_id, 'Vanilla', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO sku_field_provenance "
                "(id, sku_id, field_name, source, confidence, evidence, state, "
                "locked, created_at, updated_at) VALUES "
                "(:provenance_id, :sku_id, 'flavor', 'human', NULL, NULL, "
                "'verified', 1, :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    provenance_columns = {
        column["name"] for column in inspector.get_columns("sku_field_provenance")
    }
    with engine.connect() as connection:
        extraction_run_id = connection.scalar(
            text(
                "SELECT extraction_run_id FROM sku_field_provenance "
                "WHERE id = :provenance_id"
            ),
            {"provenance_id": identifiers["provenance_id"]},
        )
    engine.dispose()

    assert "extraction_field_reviews" in table_names
    assert "extraction_run_id" in provenance_columns
    assert extraction_run_id is None


def test_identity_migration_backfills_v1_keys_and_cleans_display_whitespace(
    tmp_path,
) -> None:
    database_path = tmp_path / "identity-backfill.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260913_0006")

    engine = create_engine(f"sqlite:///{database_path}")
    timestamp = "2026-09-14 00:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :timestamp, :timestamp)"
            ),
            {"id": "1" * 32, "name": "  ＬＡＮＤＥＲＦＩＴ  ", "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products "
                "(id, brand_id, name, created_at, updated_at) "
                "VALUES (:id, :brand_id, :name, :timestamp, :timestamp)"
            ),
            {
                "id": "2" * 32,
                "brand_id": "1" * 32,
                "name": " Premium   Whey ",
                "timestamp": timestamp,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        brand = connection.execute(
            text("SELECT name, identity_key FROM brands WHERE id = :id"),
            {"id": "1" * 32},
        ).one()
        product = connection.execute(
            text("SELECT name, identity_key FROM products WHERE id = :id"),
            {"id": "2" * 32},
        ).one()
    engine.dispose()

    assert brand.name == "ＬＡＮＤＥＲＦＩＴ"
    assert brand.identity_key == "landerfit"
    assert product.name == "Premium Whey"
    assert product.identity_key == "premium whey"


def test_identity_migration_rejects_brand_collisions_without_merging(
    tmp_path,
) -> None:
    database_path = tmp_path / "brand-collision.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260913_0006")

    engine = create_engine(f"sqlite:///{database_path}")
    timestamp = "2026-09-14 00:00:00.000000"
    with engine.begin() as connection:
        for brand_id, name in (("1" * 32, "Landerfit"), ("2" * 32, " LANDERFIT ")):
            connection.execute(
                text(
                    "INSERT INTO brands (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, :timestamp, :timestamp)"
                ),
                {"id": brand_id, "name": name, "timestamp": timestamp},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Brand collision"):
        command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM brands")) == 2
    assert "identity_key" not in {
        column["name"] for column in inspect(engine).get_columns("brands")
    }
    engine.dispose()


def test_identity_migration_rejects_scoped_product_collisions_without_merging(
    tmp_path,
) -> None:
    database_path = tmp_path / "product-collision.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260913_0006")

    engine = create_engine(f"sqlite:///{database_path}")
    timestamp = "2026-09-14 00:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands (id, name, created_at, updated_at) "
                "VALUES (:id, 'Landerfit', :timestamp, :timestamp)"
            ),
            {"id": "1" * 32, "timestamp": timestamp},
        )
        for product_id, name in (
            ("2" * 32, "Premium Whey"),
            ("3" * 32, "PREMIUM   WHEY"),
        ):
            connection.execute(
                text(
                    "INSERT INTO products "
                    "(id, brand_id, name, created_at, updated_at) "
                    "VALUES (:id, :brand_id, :name, :timestamp, :timestamp)"
                ),
                {
                    "id": product_id,
                    "brand_id": "1" * 32,
                    "name": name,
                    "timestamp": timestamp,
                },
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Product collision"):
        command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM products")) == 2
    assert "identity_key" not in {
        column["name"] for column in inspect(engine).get_columns("products")
    }
    engine.dispose()


def test_photo_intake_migration_preserves_existing_photo_rows(tmp_path) -> None:
    database_path = tmp_path / "existing-photo.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260908_0002")

    engine = create_engine(f"sqlite:///{database_path}")
    identifiers = {
        "brand_id": "1" * 32,
        "product_id": "2" * 32,
        "sku_id": "3" * 32,
        "photo_id": "4" * 32,
    }
    timestamp = "2026-09-09 00:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands (id, name, created_at, updated_at) "
                "VALUES (:brand_id, 'Legacy Brand', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products (id, brand_id, name, created_at, updated_at) "
                "VALUES (:product_id, :brand_id, 'Legacy Product', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO skus (id, product_id, created_at, updated_at) "
                "VALUES (:sku_id, :product_id, :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO photos "
                "(id, sku_id, file_path, checksum_sha256, role, is_original, created_at) "
                "VALUES (:photo_id, :sku_id, 'storage/originals/legacy.jpg', "
                ":checksum, 'front', 1, :timestamp)"
            ),
            {
                **identifiers,
                "checksum": "a" * 64,
                "timestamp": timestamp,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        migrated = connection.execute(
            text(
                "SELECT sku_id, original_filename, mime_type, "
                "file_size_bytes, width, height FROM photos WHERE id = :photo_id"
            ),
            {"photo_id": identifiers["photo_id"]},
        ).one()
    engine.dispose()

    assert migrated.sku_id == identifiers["sku_id"]
    assert migrated.original_filename is None
    assert migrated.mime_type is None
    assert migrated.file_size_bytes is None
    assert migrated.width is None
    assert migrated.height is None


def test_shared_media_migration_preserves_photos_and_enforces_single_owner(
    tmp_path,
) -> None:
    database_path = tmp_path / "shared-media.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260914_0007")

    engine = create_engine(f"sqlite:///{database_path}")
    identifiers = {
        "brand_id": "1" * 32,
        "product_id": "2" * 32,
        "sku_id": "3" * 32,
        "sku_photo_id": "4" * 32,
        "unassigned_photo_id": "5" * 32,
    }
    timestamp = "2026-09-15 00:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands "
                "(id, name, identity_key, created_at, updated_at) VALUES "
                "(:brand_id, 'Legacy Brand', 'legacy brand', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products "
                "(id, brand_id, name, identity_key, created_at, updated_at) VALUES "
                "(:product_id, :brand_id, 'Legacy Product', 'legacy product', "
                ":timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO skus (id, product_id, created_at, updated_at) "
                "VALUES (:sku_id, :product_id, :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        for photo_id, sku_id, marker in (
            (identifiers["sku_photo_id"], identifiers["sku_id"], "a"),
            (identifiers["unassigned_photo_id"], None, "b"),
        ):
            connection.execute(
                text(
                    "INSERT INTO photos "
                    "(id, sku_id, file_path, checksum_sha256, original_filename, "
                    "mime_type, file_size_bytes, width, height, role, is_original, "
                    "created_at) VALUES (:id, :sku_id, :path, :checksum, :filename, "
                    "'image/jpeg', 123, 10, 20, 'front', 1, :timestamp)"
                ),
                {
                    "id": photo_id,
                    "sku_id": sku_id,
                    "path": f"storage/originals/{marker}.jpg",
                    "checksum": marker * 64,
                    "filename": f"{marker}.jpg",
                    "timestamp": timestamp,
                },
            )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert "product_id" in {
        column["name"] for column in inspector.get_columns("photos")
    }
    assert any(
        foreign_key["referred_table"] == "products"
        and foreign_key["constrained_columns"] == ["product_id"]
        for foreign_key in inspector.get_foreign_keys("photos")
    )
    with engine.begin() as connection:
        migrated = connection.execute(
            text(
                "SELECT id, sku_id, product_id, file_path, checksum_sha256, "
                "original_filename, mime_type, file_size_bytes, width, height "
                "FROM photos ORDER BY id"
            )
        ).mappings().all()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with pytest.raises(Exception, match="ck_photos_single_owner"):
            connection.execute(
                text(
                    "UPDATE photos SET product_id = :product_id "
                    "WHERE id = :photo_id"
                ),
                {
                    "product_id": identifiers["product_id"],
                    "photo_id": identifiers["sku_photo_id"],
                },
            )
    engine.dispose()

    assert migrated[0] == {
        "id": identifiers["sku_photo_id"],
        "sku_id": identifiers["sku_id"],
        "product_id": None,
        "file_path": "storage/originals/a.jpg",
        "checksum_sha256": "a" * 64,
        "original_filename": "a.jpg",
        "mime_type": "image/jpeg",
        "file_size_bytes": 123,
        "width": 10,
        "height": 20,
    }
    assert migrated[1]["sku_id"] is None
    assert migrated[1]["product_id"] is None
    assert migrated[1]["file_path"] == "storage/originals/b.jpg"
