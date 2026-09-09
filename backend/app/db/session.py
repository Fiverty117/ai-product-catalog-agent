from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = "sqlite:///./catalog.db"


def create_sqlite_engine(database_url: str = DATABASE_URL) -> Engine:
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


engine = create_sqlite_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
