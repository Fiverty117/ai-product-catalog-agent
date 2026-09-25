from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app


def test_health() -> None:
    engine = create_engine("sqlite:///:memory:")
    def override():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_db] = override
    try:
        client = TestClient(app)
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_database_failure_without_secrets() -> None:
    class BrokenSession:
        def execute(self, _query):
            raise SQLAlchemyError("sensitive diagnostic detail")

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        response = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
