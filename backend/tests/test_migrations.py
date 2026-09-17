from alembic import command
from alembic.config import Config
import json
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_initial_migration_upgrades_clean_database(tmp_path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version",
        "brands",
        "catalog_artifacts",
        "catalog_brand_assets",
        "catalog_brand_profiles",
        "catalog_render_runs",
        "catalog_snapshots",
        "categories",
        "category_suggestion_reviews",
        "category_suggestion_runs",
        "derived_images",
        "derived_image_reviews",
        "extraction_field_reviews",
        "extraction_identity_resolutions",
        "extraction_run_photos",
        "extraction_runs",
        "image_enhancement_runs",
        "jobs",
        "photos",
        "photo_presentation_preferences",
        "prices",
        "products",
        "product_categories",
        "skus",
        "sku_field_provenance",
    }
    engine.dispose()
    command.check(config)


def _simulate_applied_0015_accent_check_typo(engine) -> None:
    correct = "#" + "[0-9A-F]" * 6
    broken = "#" + "[0-9A-F]" * 5
    with engine.connect() as connection:
        original_ddl = connection.scalar(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='catalog_brand_profiles'"
        ))
        assert original_ddl is not None
        assert f"accent_color GLOB '{correct}'" in original_ddl
        bad_ddl = original_ddl.replace(
            f"accent_color GLOB '{correct}'", f"accent_color GLOB '{broken}'", 1
        ).replace(
            "CREATE TABLE catalog_brand_profiles",
            "CREATE TABLE catalog_brand_profiles_broken", 1,
        )
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        connection.exec_driver_sql(bad_ddl)
        connection.exec_driver_sql(
            "INSERT INTO catalog_brand_profiles_broken SELECT * FROM catalog_brand_profiles"
        )
        connection.exec_driver_sql("DROP TABLE catalog_brand_profiles")
        connection.exec_driver_sql(
            "ALTER TABLE catalog_brand_profiles_broken RENAME TO catalog_brand_profiles"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_catalog_brand_profiles_logo_asset_id ON catalog_brand_profiles (logo_asset_id)"
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()


def _assert_six_hex_position_color_checks(engine) -> None:
    checks = {
        check["name"]: check["sqltext"]
        for check in inspect(engine).get_check_constraints("catalog_brand_profiles")
    }
    for name in ("ck_catalog_brand_profiles_primary_color", "ck_catalog_brand_profiles_accent_color"):
        assert "length(" in checks[name]
        assert checks[name].count("[0-9A-F]") == 6


def test_0016_repairs_applied_broken_check_without_fabricating_profiles(tmp_path) -> None:
    database_path = tmp_path / "accent-repair-empty.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260922_0015")
    engine = create_engine(f"sqlite:///{database_path}")
    _simulate_applied_0015_accent_check_typo(engine)
    original_checks = {
        check["name"]: check["sqltext"]
        for check in inspect(engine).get_check_constraints("catalog_brand_profiles")
    }
    assert connection_count(engine, "catalog_brand_profiles") == 0
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    _assert_six_hex_position_color_checks(engine)
    repaired_checks = {
        check["name"]: check["sqltext"]
        for check in inspect(engine).get_check_constraints("catalog_brand_profiles")
    }
    assert set(repaired_checks) == set(original_checks)
    for name in set(original_checks) - {"ck_catalog_brand_profiles_accent_color"}:
        assert repaired_checks[name] == original_checks[name]
    assert connection_count(engine, "catalog_brand_profiles") == 0
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO catalog_brand_profiles (id, key, display_name, logo_asset_id, primary_color, "
            "accent_color, contact_text, social_handle, is_active, created_at, updated_at) "
            "VALUES (:id, 'grabelan', 'Grabelan Natural Market', NULL, '#596B3F', '#B08A4A', "
            "NULL, NULL, 1, :at, :at)"
        ), {"id": "1" * 32, "at": "2026-09-15 00:00:00.000000"})
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    with engine.begin() as connection:
        for profile_id, key, color in (("3" * 32, "white", "#FFFFFF"),
                                       ("4" * 32, "black", "#000000")):
            connection.execute(text(
                "INSERT INTO catalog_brand_profiles (id, key, display_name, primary_color, "
                "accent_color, is_active, created_at, updated_at) VALUES "
                "(:id, :key, 'Boundary Color', :color, :color, 1, :at, :at)"
            ), {"id": profile_id, "key": key, "color": color,
                "at": "2026-09-15 00:00:00.000000"})
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="ck_catalog_brand_profiles_accent_color"):
            connection.execute(text(
                "INSERT INTO catalog_brand_profiles (id, key, display_name, primary_color, "
                "accent_color, is_active, created_at, updated_at) VALUES "
                "(:id, 'bad-accent', 'Bad Accent', '#596B3F', '#B08A4G', 1, :at, :at)"
            ), {"id": "5" * 32, "at": "2026-09-15 00:00:00.000000"})
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="ck_catalog_brand_profiles_key_slug"):
            connection.execute(text(
                "INSERT INTO catalog_brand_profiles (id, key, display_name, primary_color, "
                "accent_color, is_active, created_at, updated_at) VALUES "
                "(:id, 'BAD-KEY', 'Bad Key', '#596B3F', '#B08A4A', 1, :at, :at)"
            ), {"id": "6" * 32, "at": "2026-09-15 00:00:00.000000"})
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="catalog_brand_profiles.key"):
            connection.execute(text(
                "INSERT INTO catalog_brand_profiles (id, key, display_name, primary_color, accent_color, "
                "is_active, created_at, updated_at) VALUES (:id, 'grabelan', 'Duplicate', '#000000', "
                "'#FFFFFF', 1, :at, :at)"
            ), {"id": "2" * 32, "at": "2026-09-15 00:00:00.000000"})
    engine.dispose()


def connection_count(engine, table_name: str) -> int:
    with engine.connect() as connection:
        return connection.scalar(text(f"SELECT count(*) FROM {table_name}"))


def test_0016_preserves_populated_profile_asset_and_historical_render_links(tmp_path) -> None:
    database_path = tmp_path / "accent-repair-history.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260922_0015")
    engine = create_engine(f"sqlite:///{database_path}")
    _simulate_applied_0015_accent_check_typo(engine)
    ids = {"asset": "1" * 32, "profile": "2" * 32, "snapshot": "3" * 32,
           "run": "4" * 32, "artifact": "5" * 32}
    at = "2026-09-15 00:00:00.000000"
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.commit()
        connection.execute(text(
            "INSERT INTO catalog_brand_assets (id, file_path, checksum_sha256, mime_type, "
            "file_size_bytes, width, height, created_at) VALUES (:asset, 'storage/branding/a.png', "
            ":checksum, 'image/png', 100, 10, 10, :at)"
        ), {**ids, "checksum": "a" * 64, "at": at})
        connection.execute(text(
            "INSERT INTO catalog_brand_profiles (id, key, display_name, logo_asset_id, primary_color, "
            "accent_color, contact_text, social_handle, is_active, created_at, updated_at) "
            "VALUES (:profile, 'grabelan', 'Historical Publisher', :asset, '#596B3F', "
            "'#B08A4A', 'Old Contact', '@old', 1, :at, :at)"
        ), {**ids, "at": at})
        connection.execute(text(
            "INSERT INTO catalog_snapshots (id, schema_version, currency, as_of, payload, content_hash, "
            "created_at) VALUES (:snapshot, 'catalog-snapshot-v1', 'PYG', :at, '{}', :hash, :at)"
        ), {**ids, "hash": "b" * 64, "at": at})
        connection.execute(text(
            "INSERT INTO catalog_render_runs (id, catalog_snapshot_id, catalog_brand_profile_id, "
            "branding_schema_version, branding_hash, branding_data, template_key, template_version, "
            "template_hash, renderer_version, renderer_engine, locale, config_hash, status, "
            "started_at, completed_at, created_at) VALUES (:run, :snapshot, :profile, "
            "'catalog-branding-v1', :branding_hash, :branding_data, 'grabelan-catalog-v1', "
            "'1.0', :template_hash, 'catalog-chromium-v1', 'chromium', 'es-PY', :config_hash, "
            "'succeeded', :at, :at, :at)"
        ), {**ids, "branding_hash": "c" * 64, "branding_data": json.dumps({"display_name": "Historical Publisher"}),
            "template_hash": "d" * 64, "config_hash": "e" * 64, "at": at})
        connection.execute(text(
            "INSERT INTO catalog_artifacts (id, catalog_snapshot_id, render_run_id, media_type, "
            "file_path, checksum_sha256, file_size_bytes, page_count, created_at) "
            "VALUES (:artifact, :snapshot, :run, 'application/pdf', 'storage/catalogs/old.pdf', "
            ":checksum, 100, 1, :at)"
        ), {**ids, "checksum": "f" * 64, "at": at})
        connection.commit()
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    _assert_six_hex_position_color_checks(engine)
    inspector = inspect(engine)
    assert "ix_catalog_brand_profiles_logo_asset_id" in {
        index["name"] for index in inspector.get_indexes("catalog_brand_profiles")
    }
    assert any(fk["referred_table"] == "catalog_brand_assets" for fk in inspector.get_foreign_keys("catalog_brand_profiles"))
    assert any(fk["referred_table"] == "catalog_brand_profiles" for fk in inspector.get_foreign_keys("catalog_render_runs"))
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT key, display_name, logo_asset_id, primary_color, accent_color, contact_text, "
            "social_handle, is_active FROM catalog_brand_profiles WHERE id=:profile"
        ), ids).one() == ("grabelan", "Historical Publisher", ids["asset"],
                         "#596B3F", "#B08A4A", "Old Contact", "@old", 1)
        assert connection.execute(text(
            "SELECT catalog_brand_profile_id, branding_hash, branding_data FROM catalog_render_runs WHERE id=:run"
        ), ids).one() == (ids["profile"], "c" * 64, json.dumps({"display_name": "Historical Publisher"}))
        assert connection.scalar(text("SELECT count(*) FROM catalog_brand_assets")) == 1
        assert connection.scalar(text("SELECT count(*) FROM catalog_artifacts")) == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()
    command.check(config)


def test_catalog_branding_migration_preserves_v1_render_history(tmp_path) -> None:
    database_path = tmp_path / "catalog-branding-history.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260921_0014")
    engine = create_engine(f"sqlite:///{database_path}")
    timestamp = "2026-09-21 00:00:00.000000"
    snapshot_id, run_id, artifact_id = ("1" * 32, "2" * 32, "3" * 32)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO catalog_snapshots (id, schema_version, currency, as_of, payload, content_hash, created_at) "
            "VALUES (:id, 'catalog-snapshot-v1', 'PYG', :at, '{}', :hash, :at)"
        ), {"id": snapshot_id, "at": timestamp, "hash": "a" * 64})
        connection.execute(text(
            "INSERT INTO catalog_render_runs (id, catalog_snapshot_id, template_key, template_version, template_hash, "
            "renderer_version, renderer_engine, locale, config_hash, status, started_at, completed_at, created_at) "
            "VALUES (:id, :snapshot, 'grabelan-catalog-v1', '1.0', :template_hash, 'catalog-chromium-v1', "
            "'chromium', 'es-PY', :config_hash, 'succeeded', :at, :at, :at)"
        ), {"id": run_id, "snapshot": snapshot_id, "template_hash": "b" * 64, "config_hash": "c" * 64, "at": timestamp})
        connection.execute(text(
            "INSERT INTO catalog_artifacts (id, catalog_snapshot_id, render_run_id, media_type, file_path, "
            "checksum_sha256, file_size_bytes, page_count, created_at) "
            "VALUES (:id, :snapshot, :run, 'application/pdf', 'storage/catalogs/old.pdf', :checksum, 100, 1, :at)"
        ), {"id": artifact_id, "snapshot": snapshot_id, "run": run_id, "checksum": "d" * 64, "at": timestamp})
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT status, catalog_brand_profile_id, branding_hash, branding_data FROM catalog_render_runs WHERE id=:id"
        ), {"id": run_id}).one() == ("succeeded", None, None, None)
        assert connection.scalar(text("SELECT count(*) FROM catalog_artifacts")) == 1
        assert connection.scalar(text("SELECT count(*) FROM catalog_brand_profiles")) == 0
        assert connection.scalar(text("SELECT count(*) FROM catalog_brand_assets")) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_catalog_render_migration_preserves_snapshot_without_fabricating_output(
    tmp_path,
) -> None:
    database_path = tmp_path / "catalog-render-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260920_0013")

    timestamp = "2026-09-21 00:00:00.000000"
    snapshot_id = "1" * 32
    payload = {
        "schema_version": "catalog-snapshot-v1",
        "currency": "PYG",
        "as_of": "2026-09-20T00:00:00Z",
        "sections": [],
    }
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO catalog_snapshots "
                "(id, schema_version, currency, as_of, payload, content_hash, "
                "created_at) VALUES (:id, 'catalog-snapshot-v1', 'PYG', :time, "
                ":payload, :hash, :time)"
            ),
            {
                "id": snapshot_id,
                "time": timestamp,
                "payload": json.dumps(payload),
                "hash": "a" * 64,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert {"catalog_render_runs", "catalog_artifacts"} <= set(
        inspector.get_table_names()
    )
    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT schema_version, currency, payload, content_hash "
                "FROM catalog_snapshots WHERE id = :id"
            ),
            {"id": snapshot_id},
        ).one()
        assert stored == (
            "catalog-snapshot-v1",
            "PYG",
            json.dumps(payload),
            "a" * 64,
        )
        assert connection.scalar(text("SELECT count(*) FROM catalog_render_runs")) == 0
        assert connection.scalar(text("SELECT count(*) FROM catalog_artifacts")) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_catalog_snapshot_migration_preserves_history_without_fabrication(
    tmp_path,
) -> None:
    database_path = tmp_path / "catalog-snapshot-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260919_0012")

    timestamp = "2026-09-20 00:00:00.000000"
    brand_id = "1" * 32
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands "
                "(id, name, identity_key, created_at, updated_at) VALUES "
                "(:id, 'Historical Brand', 'historical brand', :time, :time)"
            ),
            {"id": brand_id, "time": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert "catalog_snapshots" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("catalog_snapshots")} == {
        "id",
        "schema_version",
        "currency",
        "as_of",
        "payload",
        "content_hash",
        "created_at",
    }
    assert {index["name"]: index["unique"] for index in inspector.get_indexes(
        "catalog_snapshots"
    )} == {
        "ix_catalog_snapshots_content_hash": 0,
        "ix_catalog_snapshots_created_at": 0,
    }
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT name, identity_key FROM brands WHERE id = :id"),
            {"id": brand_id},
        ).one() == ("Historical Brand", "historical brand")
        assert connection.scalar(text("SELECT count(*) FROM catalog_snapshots")) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_image_presentation_migration_preserves_historical_media_unreviewed(
    tmp_path,
) -> None:
    database_path = tmp_path / "image-enhancement-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260918_0011")

    timestamp = "2026-09-18 00:00:00.000000"
    identifiers = {
        "brand": "1" * 32,
        "product": "2" * 32,
        "sku": "3" * 32,
        "photo": "4" * 32,
        "run": "5" * 32,
        "derived": "6" * 32,
    }
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands "
                "(id, name, identity_key, created_at, updated_at) VALUES "
                "(:brand, 'Brand', 'brand', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products "
                "(id, brand_id, name, identity_key, created_at, updated_at) VALUES "
                "(:product, :brand, 'Product', 'product', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO skus (id, product_id, created_at, updated_at) VALUES "
                "(:sku, :product, :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO photos "
                "(id, sku_id, product_id, file_path, checksum_sha256, role, "
                "is_original, original_filename, mime_type, file_size_bytes, "
                "width, height, created_at) VALUES "
                "(:photo, :sku, NULL, 'storage/originals/source.png', :checksum, "
                "'front', 1, 'source.png', 'image/png', 100, 10, 20, :timestamp)"
            ),
            {
                **identifiers,
                "checksum": "a" * 64,
                "timestamp": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO image_enhancement_runs "
                "(id, source_photo_id, job_id, provider, model, prompt_version, "
                "config_version, parameters_hash, status, usage, sanitized_error, "
                "started_at, completed_at, created_at) VALUES "
                "(:run, :photo, NULL, 'openai', 'image-model', 'prompt-v1', "
                "'config-v1', :parameters_hash, 'succeeded', NULL, NULL, "
                ":timestamp, :timestamp, :timestamp)"
            ),
            {
                **identifiers,
                "parameters_hash": "b" * 64,
                "timestamp": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO derived_images "
                "(id, source_photo_id, enhancement_run_id, file_path, "
                "checksum_sha256, mime_type, file_size_bytes, width, height, "
                "created_at) VALUES (:derived, :photo, :run, "
                "'storage/processed/output.png', :checksum, 'image/png', 200, "
                "20, 30, :timestamp)"
            ),
            {**identifiers, "checksum": "c" * 64, "timestamp": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert {
        "image_enhancement_runs",
        "derived_images",
        "derived_image_reviews",
        "photo_presentation_preferences",
    } <= set(inspector.get_table_names())
    with engine.connect() as connection:
        photo = connection.execute(
            text(
                "SELECT sku_id, product_id, file_path, checksum_sha256, is_original "
                "FROM photos WHERE id = :photo"
            ),
            identifiers,
        ).one()
        assert photo == (
            identifiers["sku"],
            None,
            "storage/originals/source.png",
            "a" * 64,
            1,
        )
        derived = connection.execute(
            text(
                "SELECT source_photo_id, enhancement_run_id, file_path, "
                "checksum_sha256 FROM derived_images WHERE id = :derived"
            ),
            identifiers,
        ).one()
        assert derived == (
            identifiers["photo"],
            identifiers["run"],
            "storage/processed/output.png",
            "c" * 64,
        )
        assert connection.scalar(
            text("SELECT count(*) FROM derived_image_reviews")
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM photo_presentation_preferences")
        ) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_image_enhancement_migration_preserves_original_photo_ownership(
    tmp_path,
) -> None:
    database_path = tmp_path / "image-enhancement-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260917_0010")

    timestamp = "2026-09-18 00:00:00.000000"
    identifiers = {
        "brand": "1" * 32,
        "product": "2" * 32,
        "sku": "3" * 32,
        "photo": "4" * 32,
    }
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands "
                "(id, name, identity_key, created_at, updated_at) VALUES "
                "(:brand, 'Brand', 'brand', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products "
                "(id, brand_id, name, identity_key, created_at, updated_at) VALUES "
                "(:product, :brand, 'Product', 'product', :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO skus (id, product_id, created_at, updated_at) VALUES "
                "(:sku, :product, :timestamp, :timestamp)"
            ),
            {**identifiers, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO photos "
                "(id, sku_id, product_id, file_path, checksum_sha256, role, "
                "is_original, original_filename, mime_type, file_size_bytes, "
                "width, height, created_at) VALUES "
                "(:photo, :sku, NULL, 'storage/originals/source.png', :checksum, "
                "'front', 1, 'source.png', 'image/png', 100, 10, 20, :timestamp)"
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
    inspector = inspect(engine)
    assert {"image_enhancement_runs", "derived_images"} <= set(
        inspector.get_table_names()
    )
    with engine.connect() as connection:
        photo = connection.execute(
            text(
                "SELECT sku_id, product_id, file_path, checksum_sha256, is_original "
                "FROM photos WHERE id = :photo"
            ),
            identifiers,
        ).one()
        assert photo == (
            identifiers["sku"],
            None,
            "storage/originals/source.png",
            "a" * 64,
            1,
        )
        assert connection.scalar(
            text("SELECT count(*) FROM image_enhancement_runs")
        ) == 0
        assert connection.scalar(text("SELECT count(*) FROM derived_images")) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_category_suggestion_migration_preserves_canonical_category_origin(
    tmp_path,
) -> None:
    database_path = tmp_path / "category-suggestion-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260916_0009")

    timestamp = "2026-09-17 00:00:00.000000"
    ids = {
        "brand": "1" * 32,
        "product": "2" * 32,
        "category": "3" * 32,
        "assignment": "4" * 32,
    }
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands "
                "(id, name, identity_key, created_at, updated_at) VALUES "
                "(:id, 'Brand', 'brand', :timestamp, :timestamp)"
            ),
            {"id": ids["brand"], "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products "
                "(id, brand_id, name, identity_key, created_at, updated_at) VALUES "
                "(:id, :brand, 'Product', 'product', :timestamp, :timestamp)"
            ),
            {
                "id": ids["product"],
                "brand": ids["brand"],
                "timestamp": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO categories "
                "(id, name, identity_key, sort_order, is_active, created_at, "
                "updated_at) VALUES "
                "(:id, 'Superfoods', 'superfoods', 10, 1, :timestamp, :timestamp)"
            ),
            {"id": ids["category"], "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO product_categories "
                "(id, product_id, category_id, is_primary, source, verified, locked, "
                "created_at, updated_at) VALUES "
                "(:id, :product, :category, 1, 'human', 1, 1, :timestamp, :timestamp)"
            ),
            {
                "id": ids["assignment"],
                "product": ids["product"],
                "category": ids["category"],
                "timestamp": timestamp,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert {"category_suggestion_runs", "category_suggestion_reviews"} <= set(
        inspector.get_table_names()
    )
    assert "parameters" in {
        column["name"]
        for column in inspector.get_columns("category_suggestion_runs")
    }
    assert "category_suggestion_run_id" in {
        column["name"] for column in inspector.get_columns("product_categories")
    }
    assert "uq_product_categories_one_primary" in {
        index["name"] for index in inspector.get_indexes("product_categories")
    }
    with engine.connect() as connection:
        assignment = connection.execute(
            text(
                "SELECT product_id, category_id, is_primary, source, verified, locked, "
                "category_suggestion_run_id FROM product_categories WHERE id = :id"
            ),
            {"id": ids["assignment"]},
        ).one()
        assert assignment == (
            ids["product"],
            ids["category"],
            1,
            "human",
            1,
            1,
            None,
        )
        assert connection.scalar(text("SELECT count(*) FROM categories")) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM category_suggestion_runs")
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM category_suggestion_reviews")
        ) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


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


def test_category_migration_preserves_existing_domain_and_adds_constraints(
    tmp_path,
) -> None:
    database_path = tmp_path / "category-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260915_0008")

    engine = create_engine(f"sqlite:///{database_path}")
    ids = {
        "brand": "1" * 32,
        "product": "2" * 32,
        "sku": "3" * 32,
        "photo": "4" * 32,
    }
    timestamp = "2026-09-16 00:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO brands "
                "(id, name, identity_key, created_at, updated_at) VALUES "
                "(:id, 'Legacy Brand', 'legacy brand', :timestamp, :timestamp)"
            ),
            {"id": ids["brand"], "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO products "
                "(id, brand_id, name, identity_key, created_at, updated_at) VALUES "
                "(:id, :brand, 'Legacy Product', 'legacy product', "
                ":timestamp, :timestamp)"
            ),
            {"id": ids["product"], "brand": ids["brand"], "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO skus (id, product_id, flavor, created_at, updated_at) "
                "VALUES (:id, :product, 'Vanilla', :timestamp, :timestamp)"
            ),
            {"id": ids["sku"], "product": ids["product"], "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO photos "
                "(id, product_id, sku_id, file_path, checksum_sha256, role, "
                "is_original, created_at) VALUES "
                "(:id, :product, NULL, 'storage/originals/legacy.jpg', :checksum, "
                "'front', 1, :timestamp)"
            ),
            {
                "id": ids["photo"],
                "product": ids["product"],
                "checksum": "a" * 64,
                "timestamp": timestamp,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert {"categories", "product_categories"} <= set(inspector.get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM brands")) == 1
        assert connection.scalar(text("SELECT count(*) FROM products")) == 1
        assert connection.scalar(text("SELECT count(*) FROM skus")) == 1
        assert connection.scalar(text("SELECT count(*) FROM photos")) == 1
        assert connection.scalar(text("SELECT count(*) FROM categories")) == 0
        assert connection.scalar(text("SELECT count(*) FROM product_categories")) == 0
        photo = connection.execute(
            text("SELECT product_id, sku_id, file_path FROM photos WHERE id = :id"),
            {"id": ids["photo"]},
        ).one()
        assert photo == (
            ids["product"],
            None,
            "storage/originals/legacy.jpg",
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    category_values = {
        "id": "5" * 32,
        "name": "Superfoods",
        "key": "superfoods",
        "sort": 1000,
        "active": True,
        "timestamp": timestamp,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO categories "
                "(id, name, identity_key, sort_order, is_active, created_at, updated_at) "
                "VALUES (:id, :name, :key, :sort, :active, :timestamp, :timestamp)"
            ),
            category_values,
        )
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="categories.identity_key"):
            connection.execute(
                text(
                    "INSERT INTO categories "
                    "(id, name, identity_key, sort_order, is_active, created_at, "
                    "updated_at) VALUES (:id, 'SUPERFOODS', :key, :sort, :active, "
                    ":timestamp, :timestamp)"
                ),
                {**category_values, "id": "6" * 32},
            )

    assignment_values = {
        "id": "7" * 32,
        "product": ids["product"],
        "category": category_values["id"],
        "primary": True,
        "source": "human",
        "verified": True,
        "locked": True,
        "timestamp": timestamp,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO product_categories "
                "(id, product_id, category_id, is_primary, source, verified, locked, "
                "created_at, updated_at) VALUES (:id, :product, :category, :primary, "
                ":source, :verified, :locked, :timestamp, :timestamp)"
            ),
            assignment_values,
        )
    with engine.begin() as connection:
        with pytest.raises(
            IntegrityError,
            match="product_categories.product_id, product_categories.category_id",
        ):
            connection.execute(
                text(
                    "INSERT INTO product_categories "
                    "(id, product_id, category_id, is_primary, source, verified, "
                    "locked, created_at, updated_at) VALUES "
                    "(:id, :product, :category, 0, :source, :verified, :locked, "
                    ":timestamp, :timestamp)"
                ),
                {**assignment_values, "id": "8" * 32},
            )

    second_category = {
        **category_values,
        "id": "9" * 32,
        "name": "Adaptogens",
        "key": "adaptogens",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO categories "
                "(id, name, identity_key, sort_order, is_active, created_at, updated_at) "
                "VALUES (:id, :name, :key, :sort, :active, :timestamp, :timestamp)"
            ),
            second_category,
        )
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="product_categories.product_id"):
            connection.execute(
                text(
                    "INSERT INTO product_categories "
                    "(id, product_id, category_id, is_primary, source, verified, "
                    "locked, created_at, updated_at) VALUES "
                    "(:id, :product, :category, 1, :source, :verified, :locked, "
                    ":timestamp, :timestamp)"
                ),
                {
                    **assignment_values,
                    "id": "a" * 32,
                    "category": second_category["id"],
                },
            )
    engine.dispose()
