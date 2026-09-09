from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_initial_migration_upgrades_clean_database(tmp_path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version",
        "brands",
        "photos",
        "prices",
        "products",
        "skus",
        "sku_field_provenance",
    }
    engine.dispose()

    command.check(config)
