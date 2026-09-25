"""Optional Build lineage upgrades old rows without inventing provenance."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _insert_build(connection, number: int, *, source: str | None = None) -> str:
    build_id = f"{number}" * 32
    snapshot_id = f"{number + 2}" * 32
    job_id = f"{number + 4}" * 32
    timestamp = "2026-09-25 12:00:00.000000"
    connection.execute(text("""
        INSERT INTO catalog_snapshots (id, schema_version, currency, as_of, payload, content_hash, created_at)
        VALUES (:id, 'catalog-snapshot-v1', 'PYG', :time, '{}', :hash, :time)
    """), {"id": snapshot_id, "time": timestamp, "hash": "a" * 64})
    connection.execute(text("""
        INSERT INTO jobs (id, job_type, status, payload, idempotency_key, attempts, max_attempts, created_at, updated_at)
        VALUES (:id, 'catalog.render.v5', 'queued', '{}', :key, 0, 3, :time, :time)
    """), {"id": job_id, "key": f"migration-job-{number}", "time": timestamp})
    columns = "id, idempotency_key, request_hash, catalog_snapshot_id, job_id, created_at"
    values = ":id, :key, :hash, :snapshot, :job, :time"
    if source is not None:
        columns += ", source_build_id"
        values += ", :source"
    connection.execute(text(f"INSERT INTO catalog_builds ({columns}) VALUES ({values})"), {
        "id": build_id, "key": f"migration-build-{number}", "hash": "b" * 64,
        "snapshot": snapshot_id, "job": job_id, "time": timestamp, "source": source,
    })
    return build_id


def test_lineage_migration_preserves_existing_builds_and_restricts_source_deletion(tmp_path):
    database = tmp_path / "catalog-lineage.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "20261003_0026")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        old_id = _insert_build(connection, 1)
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    columns = {column["name"]: column for column in inspect(engine).get_columns("catalog_builds")}
    assert columns["source_build_id"]["nullable"]
    foreign_keys = inspect(engine).get_foreign_keys("catalog_builds")
    assert any(foreign_key["constrained_columns"] == ["source_build_id"] and
               foreign_key["referred_table"] == "catalog_builds" and
               foreign_key["options"].get("ondelete") == "RESTRICT" for foreign_key in foreign_keys)
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT source_build_id FROM catalog_builds WHERE id = :id"), {"id": old_id}) is None
        new_id = _insert_build(connection, 2, source=old_id)
        assert connection.scalar(text("SELECT source_build_id FROM catalog_builds WHERE id = :id"), {"id": new_id}) == old_id
        assert connection.scalar(text("SELECT source_build_id FROM catalog_builds WHERE id = :id"), {"id": old_id}) is None
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()
    command.check(config)
