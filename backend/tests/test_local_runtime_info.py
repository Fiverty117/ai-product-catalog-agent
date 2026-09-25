import json
import os
import subprocess
import sys
from pathlib import Path

from app.db.session import effective_database_url
from app.scripts import run_catalog_render_worker
from app.scripts.local_runtime_info import resolved_database_path


def test_effective_database_url_is_shared_with_representative_worker() -> None:
    assert run_catalog_render_worker.effective_database_url is effective_database_url


def test_runtime_info_resolves_api_and_migration_without_creating_database(tmp_path: Path) -> None:
    backend = Path(__file__).resolve().parents[1]
    missing_database = tmp_path / "not-created.db"
    environment = dict(os.environ, DATABASE_URL=f"sqlite:///{missing_database.as_posix()}")
    result = subprocess.run(
        [sys.executable, "-m", "app.scripts.local_runtime_info"],
        cwd=backend, env=environment, capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)
    assert Path(info["database_path"]) == missing_database
    assert info["database_path"] == info["api_database_path"]
    assert info["alembic_head"]
    assert info["migration_status"] == "required"
    assert not missing_database.exists()
    assert resolved_database_path(environment["DATABASE_URL"]) == missing_database


def test_alembic_uses_same_environment_database_as_api(tmp_path: Path) -> None:
    backend = Path(__file__).resolve().parents[1]
    database = tmp_path / "launcher.db"
    environment = dict(os.environ, DATABASE_URL=f"sqlite:///{database.as_posix()}")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend, env=environment, capture_output=True, text=True, check=True,
    )
    result = subprocess.run(
        [sys.executable, "-m", "app.scripts.local_runtime_info"],
        cwd=backend, env=environment, capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)
    assert database.is_file()
    assert info["database_path"] == info["api_database_path"] == str(database)
    assert info["database_revision"] == info["alembic_head"]
    assert info["migration_status"] == "current"
