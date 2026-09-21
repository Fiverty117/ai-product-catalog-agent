import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, Brand, Job, Product, ProductCopyRun, ProductCopyReview, ProductCopyManualRevision, SKU
from app.db.session import create_sqlite_engine, get_db
from app.domain.enums import JobStatus
from app.domain.schemas import ProductCopyJobPayload
from app.main import app
from app.services.product_copy import (
    PRODUCT_COPY_JOB_TYPE,
    PRODUCT_COPY_SCHEMA_VERSION,
    build_product_copy_input_snapshot,
    build_product_copy_source_fingerprint,
    create_running_product_copy_run,
    enqueue_product_copy,
    mark_product_copy_run_failed,
    mark_product_copy_run_succeeded,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def editorial_store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'editorial-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield factory, client
    app.dependency_overrides.clear()
    engine.dispose()


def _make_product(session: Session, *, name: str = "Premium Whey") -> Product:
    product = Product(name=name, brand=Brand(name=f"Brand {uuid.uuid4()}"))
    session.add(product)
    session.flush()
    session.add(SKU(product=product, flavor="Vanilla"))
    session.flush()
    return product


def _payload(session: Session, product: Product) -> ProductCopyJobPayload:
    snapshot = build_product_copy_input_snapshot(session, product.id)
    return ProductCopyJobPayload(
        product_id=product.id,
        copy_type="short_description",
        provider="openai",
        model="gpt-5.6-sol",
        prompt_version="product-copy-v1",
        schema_version=PRODUCT_COPY_SCHEMA_VERSION,
        parameters={"reasoning_effort": "low"},
        source_fingerprint=build_product_copy_source_fingerprint(snapshot),
        input_snapshot=snapshot,
    )


def _succeeded_run(
    session: Session,
    product: Product,
    text: str,
    *,
    created_at: datetime,
    job_id: uuid.UUID | None = None,
) -> ProductCopyRun:
    run = create_running_product_copy_run(
        session,
        payload=_payload(session, product),
        job_id=job_id,
        started_at=created_at,
    )
    run.created_at = created_at
    return mark_product_copy_run_succeeded(
        session,
        run,
        structured_result={"short_description": text},
        completed_at=created_at,
    )


def test_editorial_summary_none_current_stale_and_newest_first(editorial_store) -> None:
    factory, client = editorial_store
    with factory() as session:
        product = _make_product(session)
        original_name = product.name
        session.commit()
        product_id = product.id

    initial = client.get(f"/api/products/{product_id}/product-copy")
    assert initial.status_code == 200
    assert initial.json()["effective_copy"] == {
        "state": "none",
        "short_description": None,
    }

    with factory() as session:
        product = session.get(Product, product_id)
        reviewed = _succeeded_run(
            session,
            product,
            "Existing approved text.",
            created_at=NOW,
        )
        session.commit()
        reviewed_id = reviewed.id

    approved = client.post(
        f"/api/products/{product_id}/product-copy/runs/{reviewed_id}/review",
        json={"decision": "approved"},
    )
    assert approved.status_code == 200
    assert approved.json()["editorial"]["effective_copy"] == {
        "state": "current",
        "short_description": "Existing approved text.",
    }

    with factory() as session:
        product = session.get(Product, product_id)
        newer = _succeeded_run(
            session,
            product,
            "Alternative proposed text.",
            created_at=NOW + timedelta(minutes=1),
        )
        session.commit()
        newer_id = newer.id

    current_with_proposal = client.get(f"/api/products/{product_id}/product-copy")
    payload = current_with_proposal.json()
    assert payload["effective_copy"]["short_description"] == "Existing approved text."
    assert [row["run_id"] for row in payload["runs"]] == [
        str(newer_id),
        str(reviewed_id),
    ]
    assert payload["runs"][0]["review_state"] == "unreviewed"
    assert payload["runs"][1]["review_state"] == "approved"

    with factory() as session:
        product = session.get(Product, product_id)
        product.name = "Premium Whey Updated"
        session.commit()

    stale = client.get(f"/api/products/{product_id}/product-copy").json()
    assert stale["effective_copy"] == {
        "state": "stale",
        "short_description": None,
    }
    assert all(row["source_state"] == "stale" for row in stale["runs"])
    with factory() as session:
        assert session.get(Product, product_id).name != original_name


def test_generation_enqueue_is_idempotent_and_does_not_mutate_product(
    editorial_store,
) -> None:
    factory, client = editorial_store
    with factory() as session:
        product = _make_product(session)
        session.commit()
        product_id = product.id
        original_name = product.name

    headers = {"Idempotency-Key": "same-editorial-action"}
    first = client.post(
        f"/api/products/{product_id}/product-copy/generations",
        headers=headers,
    )
    second = client.post(
        f"/api/products/{product_id}/product-copy/generations",
        headers=headers,
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["generation"]["job_id"] == second.json()["generation"]["job_id"]
    assert first.json()["generation"]["status"] == "queued"
    assert first.json()["editorial"]["has_active_generation"] is True
    with factory() as session:
        assert session.scalar(
            select(func.count(Job.id)).where(Job.job_type == PRODUCT_COPY_JOB_TYPE)
        ) == 1
        assert session.get(Product, product_id).name == original_name

    deliberate_new = client.post(
        f"/api/products/{product_id}/product-copy/generations",
        headers={"Idempotency-Key": "later-deliberate-action"},
    )
    assert deliberate_new.status_code == 202
    assert deliberate_new.json()["generation"]["job_id"] != first.json()["generation"]["job_id"]
    with factory() as session:
        assert session.scalar(
            select(func.count(Job.id)).where(Job.job_type == PRODUCT_COPY_JOB_TYPE)
        ) == 2

    missing = client.post(f"/api/products/{uuid.uuid4()}/product-copy/generations")
    assert missing.status_code == 404


def test_approve_correct_reject_validation_and_conflicts(editorial_store) -> None:
    factory, client = editorial_store
    with factory() as session:
        product = _make_product(session)
        runs = [
            _succeeded_run(
                session,
                product,
                text,
                created_at=NOW + timedelta(minutes=index),
            )
            for index, text in enumerate(
                (
                    "Proposal to approve.",
                    "Proposal to correct.",
                    "Proposal to reject.",
                    "Proposal with invalid correction.",
                )
            )
        ]
        session.commit()
        product_id = product.id
        run_ids = [run.id for run in runs]

    approved = client.post(
        f"/api/products/{product_id}/product-copy/runs/{run_ids[0]}/review",
        json={"decision": "approved"},
    )
    corrected = client.post(
        f"/api/products/{product_id}/product-copy/runs/{run_ids[1]}/review",
        json={
            "decision": "corrected",
            "corrected_short_description": "  Corrected   final copy. ",
        },
    )
    rejected = client.post(
        f"/api/products/{product_id}/product-copy/runs/{run_ids[2]}/review",
        json={"decision": "rejected"},
    )

    assert approved.status_code == corrected.status_code == rejected.status_code == 200
    assert approved.json()["review"]["decision"] == "approved"
    assert corrected.json()["review"]["corrected_short_description"] == (
        "Corrected final copy."
    )
    assert corrected.json()["editorial"]["effective_copy"]["short_description"] == (
        "Corrected final copy."
    )
    assert rejected.json()["review"]["decision"] == "rejected"

    duplicate = client.post(
        f"/api/products/{product_id}/product-copy/runs/{run_ids[0]}/review",
        json={"decision": "rejected"},
    )
    assert duplicate.status_code == 409
    invalid = client.post(
        f"/api/products/{product_id}/product-copy/runs/{run_ids[3]}/review",
        json={"decision": "corrected", "corrected_short_description": "x" * 181},
    )
    assert invalid.status_code == 422
    missing_run = client.post(
        f"/api/products/{product_id}/product-copy/runs/{uuid.uuid4()}/review",
        json={"decision": "approved"},
    )
    assert missing_run.status_code == 404
    missing_product = client.get(f"/api/products/{uuid.uuid4()}/product-copy")
    assert missing_product.status_code == 404


def test_failed_generation_and_run_are_safe_and_retryable(editorial_store) -> None:
    factory, client = editorial_store
    with factory() as session:
        product = _make_product(session)
        job = enqueue_product_copy(session, product_id=product.id)
        job.status = JobStatus.FAILED
        job.attempts = 1
        job.last_error = "PermanentProductCopyProviderError: credentials unavailable"
        job.finished_at = NOW
        failed_run = create_running_product_copy_run(
            session,
            payload=ProductCopyJobPayload.model_validate(job.payload),
            job_id=job.id,
            started_at=NOW,
        )
        mark_product_copy_run_failed(
            session,
            failed_run,
            error="api_key=sk-sensitive provider failure",
            completed_at=NOW,
        )
        session.commit()
        product_id = product.id
        job_id = job.id

    summary = client.get(f"/api/products/{product_id}/product-copy").json()
    assert summary["generations"][0]["status"] == "failed"
    assert summary["generations"][0]["can_retry"] is True
    assert summary["runs"][0]["status"] == "failed"
    assert "sk-sensitive" not in summary["runs"][0]["sanitized_error"]

    retried = client.post(
        f"/api/products/{product_id}/product-copy/generations/{job_id}/retry"
    )
    assert retried.status_code == 202
    assert retried.json()["generation"]["status"] == "queued"
    assert retried.json()["editorial"]["has_active_generation"] is True


def test_manual_revision_api_is_append_only_and_current_only(editorial_store) -> None:
    factory, client = editorial_store
    with factory() as session:
        product = _make_product(session)
        run = _succeeded_run(session, product, "AI copy.", created_at=NOW)
        session.commit()
        product_id, run_id = product.id, run.id
    path = f"/api/products/{product_id}/product-copy/manual-revisions"
    assert client.post(path, json={"short_description": "Too early."}).status_code == 409
    assert client.post(f"/api/products/{product_id}/product-copy/runs/{run_id}/review", json={"decision": "approved"}).status_code == 200
    for text in ("", " ", "x" * 181):
        assert client.post(path, json={"short_description": text}).status_code == 422
    saved = client.post(path, json={"short_description": "  Human   copy. "})
    assert saved.status_code == 201
    assert saved.json()["editorial"]["effective_copy"] == {"state": "current", "short_description": "Human copy."}
    assert saved.json()["editorial"]["manual_revisions"][0]["short_description"] == "Human copy."
    assert client.post(path, json={"short_description": "Next copy."}).status_code == 201
    with factory() as session:
        assert session.get(Product, product_id).name == "Premium Whey"
        assert session.get(ProductCopyRun, run_id).generated_text == "AI copy."
        assert session.scalar(select(func.count(ProductCopyReview.id))) == 1
        assert session.scalar(select(func.count(ProductCopyManualRevision.id))) == 2
        session.get(Product, product_id).name = "Renamed"
        session.commit()
    assert client.get(f"/api/products/{product_id}/product-copy").json()["effective_copy"]["state"] == "stale"
    assert client.post(path, json={"short_description": "Cannot edit stale."}).status_code == 409
    assert client.post(f"/api/products/{uuid.uuid4()}/product-copy/manual-revisions", json={"short_description": "Valid."}).status_code == 404
