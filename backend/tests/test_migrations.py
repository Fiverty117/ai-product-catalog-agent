from alembic import command
from alembic.config import Config
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
