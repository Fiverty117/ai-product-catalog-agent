"""Read-only local SQLite and Alembic diagnostics for the Windows launcher."""

import json
import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.db.session import effective_database_url, engine


def resolved_database_path(url: str) -> Path:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        raise ValueError("Local launcher requires a file-backed SQLite DATABASE_URL")
    return Path(parsed.database).resolve()


def read_runtime_info() -> dict[str, str | None]:
    database_path = resolved_database_path(effective_database_url())
    api_path = resolved_database_path(str(engine.url))
    config = Config(str(Path.cwd() / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    revision = None
    if database_path.is_file():
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
        try:
            try:
                row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                revision = row[0] if row else None
            except sqlite3.OperationalError:
                pass
        finally:
            connection.close()
    return {
        "database_path": str(database_path),
        "api_database_path": str(api_path),
        "alembic_head": head,
        "database_revision": revision,
        "migration_status": "current" if revision == head else "required",
    }


if __name__ == "__main__":
    print(json.dumps(read_runtime_info(), sort_keys=True))
