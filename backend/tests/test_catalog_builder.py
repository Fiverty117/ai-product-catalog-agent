import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, Brand, CatalogBrandProfile, Photo, Price, Product, SKU
from app.db.session import create_sqlite_engine, get_db
from app.domain.enums import (
    DerivedImageReviewDecision,
    PhotoRole,
    ProductCopyReviewDecision,
)
from app.domain.schemas import (
    DerivedImageReviewCreate,
    ImageEnhancementJobPayload,
    ProductCopyJobPayload,
    ProductCopyReviewRequest,
)
from app.main import app
from app.services.catalog_builder import (
    list_catalog_builder_brand_profiles,
    list_catalog_builder_layouts,
    list_catalog_builder_products,
)
from app.services.categories import assign_product_category, create_category
from app.services.image_enhancement import (
    complete_image_enhancement_run,
    create_running_image_enhancement_run,
    store_processed_image,
)
from app.services.image_presentation import (
    create_derived_image_review,
    select_derived_image_for_photo,
)
from app.services.product_copy import (
    PRODUCT_COPY_SCHEMA_VERSION,
    build_product_copy_input_snapshot,
    build_product_copy_source_fingerprint,
    create_running_product_copy_run,
    mark_product_copy_run_succeeded,
)
from app.services.product_copy_review import apply_product_copy_review

AS_OF = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def builder_store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'builder.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield factory, client, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()


def _image_bytes(color=(238, 240, 232)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 48), color).save(output, format="PNG")
    return output.getvalue()


def _add_front_photo(session: Session, product: Product, path) -> Photo:
    content = _image_bytes()
    path.write_bytes(content)
    photo = Photo(
        product=product,
        file_path=str(path),
        checksum_sha256=hashlib.sha256(content).hexdigest(),
        original_filename="front.png",
        mime_type="image/png",
        file_size_bytes=len(content),
        width=32,
        height=48,
        role=PhotoRole.FRONT,
        is_original=True,
    )
    session.add(photo)
    session.flush()
    return photo


def _add_price(
    session: Session,
    sku: SKU,
    amount: str,
    *,
    valid_from: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc),
    approved: bool = True,
) -> Price:
    price = Price(
        sku=sku,
        amount=Decimal(amount),
        currency="PYG",
        valid_from=valid_from,
        source="manual",
        approved=approved,
    )
    session.add(price)
    session.flush()
    return price


def _add_reviewed_copy(session: Session, product: Product, text: str) -> None:
    snapshot = build_product_copy_input_snapshot(session, product.id)
    payload = ProductCopyJobPayload(
        product_id=product.id,
        copy_type="short_description",
        provider="test-provider",
        model="test-model",
        prompt_version="product-copy-v1",
        schema_version=PRODUCT_COPY_SCHEMA_VERSION,
        parameters={},
        source_fingerprint=build_product_copy_source_fingerprint(snapshot),
        input_snapshot=snapshot,
    )
    run = create_running_product_copy_run(session, payload=payload, started_at=AS_OF)
    mark_product_copy_run_succeeded(
        session,
        run,
        structured_result={"short_description": text},
        completed_at=AS_OF,
    )
    apply_product_copy_review(
        session,
        ProductCopyReviewRequest(
            product_copy_run_id=run.id,
            decision=ProductCopyReviewDecision.APPROVED,
        ),
        applied_at=AS_OF,
    )


def _select_derived_image(session: Session, photo: Photo, tmp_path):
    payload = ImageEnhancementJobPayload(
        source_photo_id=photo.id,
        source_checksum_sha256=photo.checksum_sha256,
        provider="test-provider",
        model="test-image-model",
        prompt_version="product-image-enhancement-v1",
        config_version="image-enhancement-config-v1",
        parameters={
            "quality": "high",
            "output_format": "png",
            "size": "auto",
            "background": "auto",
        },
    )
    run = create_running_image_enhancement_run(session, payload=payload)
    content = _image_bytes((72, 116, 88))
    stored = store_processed_image(content, processed_dir=tmp_path / "processed")
    derived = complete_image_enhancement_run(session, run, stored_image=stored)
    create_derived_image_review(
        session,
        DerivedImageReviewCreate(
            derived_image_id=derived.id,
            decision=DerivedImageReviewDecision.APPROVED,
        ),
    )
    select_derived_image_for_photo(
        session,
        photo_id=photo.id,
        derived_image_id=derived.id,
    )
    return derived, content


def _make_ready_product(session: Session, tmp_path) -> tuple[Product, list[SKU], Photo]:
    product = Product(
        id=uuid.UUID(int=20),
        name="Premium Whey",
        brand=Brand(name="Landerfit"),
    )
    session.add(product)
    session.flush()
    category = create_category(session, name="Proteínas", sort_order=10)
    assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    skus = [
        SKU(
            id=uuid.UUID(int=2),
            product=product,
            flavor="Chocolate",
            size_value=Decimal("2"),
            size_unit="LB",
        ),
        SKU(
            id=uuid.UUID(int=1),
            product=product,
            flavor="Vanilla",
            size_value=Decimal("2"),
            size_unit="LB",
        ),
    ]
    session.add_all(skus)
    session.flush()
    _add_price(session, skus[0], "340000")
    _add_price(session, skus[1], "350000")
    _add_price(
        session,
        skus[1],
        "999999",
        valid_from=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    _add_price(session, skus[1], "1", approved=False)
    photo = _add_front_photo(session, product, tmp_path / "landerfit-front.png")
    _add_reviewed_copy(
        session,
        product,
        "Proteína premium disponible en sabores Vanilla y Chocolate.",
    )
    return product, skus, photo


def test_builder_product_read_model_preserves_authoritative_state(builder_store) -> None:
    factory, _, tmp_path = builder_store
    with factory() as session:
        ready, _, photo = _make_ready_product(session, tmp_path)
        blocked = Product(name="Needs Review", brand=Brand(name="Acme"))
        session.add(blocked)
        session.flush()
        session.add(SKU(product=blocked, flavor="Plain"))
        session.commit()

        result = list_catalog_builder_products(session, as_of=AS_OF)

        assert [item.product_name for item in result.products] == [
            "Needs Review",
            "Premium Whey",
        ]
        blocked_summary, ready_summary = result.products
        assert blocked_summary.readiness.ready is False
        assert {item.code.value for item in blocked_summary.readiness.blockers} >= {
            "missing_primary_category",
            "missing_catalog_front_photo",
            "no_publishable_skus",
        }
        assert blocked_summary.copy_state.value == "none"
        assert blocked_summary.short_description is None

        assert ready_summary.product_id == ready.id
        assert ready_summary.brand_name == "Landerfit"
        assert ready_summary.primary_category_name == "Proteínas"
        assert ready_summary.readiness.ready is True
        assert [item.variant_label for item in ready_summary.publishable_skus] == [
            "Vanilla / 2 LB",
            "Chocolate / 2 LB",
        ]
        assert [item.active_price_amount for item in ready_summary.publishable_skus] == [
            Decimal("350000"),
            Decimal("340000"),
        ]
        assert ready_summary.hero.source_photo_id == photo.id
        assert ready_summary.hero.presentation_type.value == "original"
        assert ready_summary.hero.image_url.endswith(f"/{ready.id}/image")
        assert ready_summary.copy_state.value == "current"
        assert ready_summary.short_description.startswith("Proteína premium")

        filtered = list_catalog_builder_products(
            session,
            search="LANDERFIT",
            readiness="ready",
            as_of=AS_OF,
        )
        assert [item.product_id for item in filtered.products] == [ready.id]
        assert list_catalog_builder_products(
            session,
            search="no match",
            as_of=AS_OF,
        ).products == []


def test_builder_copy_stale_is_distinct_and_hides_stale_text(builder_store) -> None:
    factory, _, tmp_path = builder_store
    with factory() as session:
        product, skus, _ = _make_ready_product(session, tmp_path)
        skus[0].servings = 42
        session.flush()

        summary = list_catalog_builder_products(session, as_of=AS_OF).products[0]

        assert summary.product_id == product.id
        assert summary.copy_state.value == "stale"
        assert summary.short_description is None


def test_builder_brand_profiles_layout_registry_and_media_api(builder_store) -> None:
    factory, client, tmp_path = builder_store
    with factory() as session:
        product, _, photo = _make_ready_product(session, tmp_path)
        derived, derived_content = _select_derived_image(session, photo, tmp_path)
        active = CatalogBrandProfile(
            key="grabelan",
            display_name="Grabelan Natural Market",
            primary_color="#183D2F",
            accent_color="#C89B3C",
            is_active=True,
        )
        inactive = CatalogBrandProfile(
            key="old-brand",
            display_name="Old Brand",
            primary_color="#111111",
            accent_color="#222222",
            is_active=False,
        )
        session.add_all([inactive, active])
        session.commit()

        profiles = list_catalog_builder_brand_profiles(session)
        layouts = list_catalog_builder_layouts()
        summary = list_catalog_builder_products(session, as_of=AS_OF).products[0]

        assert [(item.key, item.display_name) for item in profiles] == [
            ("grabelan", "Grabelan Natural Market")
        ]
        assert profiles[0].logo_url is None
        assert summary.hero.presentation_type.value == "derived"
        assert summary.hero.effective_derived_image_id == derived.id
        assert [
            (item.key, item.version, item.products_per_row)
            for item in layouts
        ] == [
            ("classic", "1", 2),
            ("dense", "1", 3),
            ("compact", "1", 4),
        ]

    response = client.get("/api/catalog-builder/products?readiness=ready")
    assert response.status_code == 200
    assert response.json()["products"][0]["product_name"] == "Premium Whey"
    image = client.get(f"/api/catalog-builder/products/{product.id}/image")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == derived_content
    profiles_response = client.get("/api/catalog-builder/brand-profiles")
    assert [item["key"] for item in profiles_response.json()] == ["grabelan"]
    assert [item["key"] for item in client.get("/api/catalog-builder/layouts").json()] == [
        "classic",
        "dense",
        "compact",
    ]
