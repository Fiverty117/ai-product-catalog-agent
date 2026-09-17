import hashlib
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base, Brand, CatalogBrandAsset, CatalogBrandProfile
from app.db.session import create_sqlite_engine
from app.domain.schemas import (
    CatalogBrandProfileCreate,
    CatalogBrandProfileUpdate,
    FrozenCatalogBrandLogo,
    ResolvedCatalogBranding,
)
from app.scripts.catalog_visual_stress import build_stress_view_model
from app.scripts.manual_catalog_brand_profile import build_parser as brand_profile_parser
from app.scripts.manual_catalog_render import build_parser as catalog_render_parser
from app.services.catalog_branding import (
    CatalogBrandingError,
    CatalogBrandLogoIntegrityError,
    InactiveCatalogBrandProfileError,
    create_catalog_brand_profile,
    get_active_catalog_brand_profile,
    hash_resolved_catalog_branding,
    ingest_catalog_brand_logo,
    load_frozen_catalog_brand_logo,
    resolve_catalog_branding,
    update_catalog_brand_profile,
)
from app.services.catalog_rendering import (
    build_catalog_branding_view,
    render_catalog_html,
    resolve_catalog_template,
)

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "grabelan"


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'brands.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory, tmp_path / "storage"
    engine.dispose()


def image_bytes(format_name):
    output = BytesIO()
    Image.new("RGB", (19, 13), (72, 89, 112)).save(output, format=format_name)
    return output.getvalue()


def profile_data(key="grabelan", **changes):
    return {
        "key": key, "display_name": "Grabelan", "primary_color": "#1a2b3c",
        "accent_color": "#AABBCC", "contact_text": None, "social_handle": None,
        **changes,
    }


def test_text_profile_identity_color_update_deactivation_and_product_brand_separation(store):
    factory, storage_root = store
    with factory() as session:
        product_brand = Brand(name="Landerfit")
        session.add(product_brand)
        profile = create_catalog_brand_profile(session, profile_data())
        assert profile.primary_color == "#1A2B3C"
        resolved = resolve_catalog_branding(profile, storage_root=storage_root)
        assert resolved.logo is None
        assert resolved.display_name == "Grabelan"
        assert profile.id != product_brand.id
        initial_hash = hash_resolved_catalog_branding(resolved)
        assert hash_resolved_catalog_branding(resolved) == initial_hash
        update_catalog_brand_profile(session, profile, {"display_name": "Grabelan Nuevo", "accent_color": "#123abc", "contact_text": "Contacto de prueba"})
        changed = resolve_catalog_branding(profile, storage_root=storage_root)
        assert changed.display_name == "Grabelan Nuevo"
        assert changed.accent_color == "#123ABC"
        assert hash_resolved_catalog_branding(changed) != initial_hash
        update_catalog_brand_profile(session, profile, {"is_active": False})
        with pytest.raises(InactiveCatalogBrandProfileError):
            get_active_catalog_brand_profile(session, key="grabelan")
        with pytest.raises(InactiveCatalogBrandProfileError):
            resolve_catalog_branding(profile, storage_root=storage_root)


def test_profile_key_unique_and_color_css_boundary(store):
    factory, _ = store
    for key in ("GRABELAN", "client_x", "-client", "client--x", " client"):
        with pytest.raises(ValidationError):
            CatalogBrandProfileCreate.model_validate(profile_data(key=key))
    for color in ("red", "#123", "#1234567", "var(--x)", "url(https://x)", "#12<456"):
        with pytest.raises(ValidationError):
            CatalogBrandProfileCreate.model_validate(profile_data(primary_color=color))
    with pytest.raises(ValidationError):
        CatalogBrandProfileUpdate.model_validate({"primary_color": None})
    with factory() as session:
        create_catalog_brand_profile(session, profile_data())
        with pytest.raises(IntegrityError):
            create_catalog_brand_profile(session, profile_data(display_name="Other"))
        session.rollback()


@pytest.mark.parametrize("primary,accent", [
    ("#596B3F", "#B08A4A"),
    ("#FFFFFF", "#FFFFFF"),
    ("#000000", "#000000"),
])
def test_six_digit_colors_persist_under_orm_database_constraints(store, primary, accent):
    factory, _ = store
    with factory() as session:
        profile = create_catalog_brand_profile(session, profile_data(
            primary_color=primary, accent_color=accent,
        ))
        session.commit()
        assert (profile.primary_color, profile.accent_color) == (primary, accent)
    with factory() as session:
        stored = session.get(CatalogBrandProfile, profile.id)
        assert (stored.primary_color, stored.accent_color) == (primary, accent)


@pytest.mark.parametrize("format_name,mime,extension", [
    ("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg"),
    ("WEBP", "image/webp", ".webp"),
])
def test_logo_ingest_content_addressing_and_frozen_resolution(store, format_name, mime, extension):
    factory, storage_root = store
    content = image_bytes(format_name)
    checksum = hashlib.sha256(content).hexdigest()
    with factory() as session:
        asset = ingest_catalog_brand_logo(session, content, storage_root=storage_root)
        same = ingest_catalog_brand_logo(session, content, storage_root=storage_root)
        assert same.id == asset.id
        assert asset.mime_type == mime
        assert asset.file_path == str(storage_root.resolve() / "branding" / checksum[:2] / f"{checksum}{extension}")
        assert (asset.file_size_bytes, asset.width, asset.height) == (len(content), 19, 13)
        profile = create_catalog_brand_profile(session, profile_data(), logo_asset=asset, storage_root=storage_root)
        resolved = resolve_catalog_branding(profile, storage_root=storage_root)
        assert resolved.logo.storage_relative_path == f"branding/{checksum[:2]}/{checksum}{extension}"
        assert "C:" not in str(resolved.model_dump(mode="json"))
        assert load_frozen_catalog_brand_logo(resolved.logo, storage_root=storage_root) == content
        view = build_catalog_branding_view(resolved, storage_root=storage_root)
        assert view.logo_data_uri.startswith(f"data:{mime};base64,")
        assert hash_resolved_catalog_branding(resolved) == hash_resolved_catalog_branding(resolved)
        assert session.query(CatalogBrandAsset).count() == 1


def test_invalid_missing_and_corrupt_logo_rejected_without_live_fallback(store):
    factory, storage_root = store
    with factory() as session:
        with pytest.raises(CatalogBrandLogoIntegrityError):
            ingest_catalog_brand_logo(session, b"not an image", storage_root=storage_root)
        asset = ingest_catalog_brand_logo(session, image_bytes("PNG"), storage_root=storage_root)
        profile = create_catalog_brand_profile(session, profile_data(), logo_asset=asset, storage_root=storage_root)
        frozen = resolve_catalog_branding(profile, storage_root=storage_root)
        path = Path(asset.file_path)
        path.write_bytes(b"corrupt")
        with pytest.raises(CatalogBrandLogoIntegrityError, match="checksum"):
            load_frozen_catalog_brand_logo(frozen.logo, storage_root=storage_root)
        with pytest.raises(CatalogBrandLogoIntegrityError):
            resolve_catalog_branding(profile, storage_root=storage_root)
        path.unlink()
        with pytest.raises(CatalogBrandLogoIntegrityError, match="missing"):
            load_frozen_catalog_brand_logo(frozen.logo, storage_root=storage_root)


def test_visual_hash_excludes_profile_uuid_but_tracks_render_values(store):
    factory, storage_root = store
    with factory() as session:
        first = create_catalog_brand_profile(session, profile_data("grabelan"))
        second = create_catalog_brand_profile(session, profile_data("gravefit"))
        a = resolve_catalog_branding(first, storage_root=storage_root)
        b = resolve_catalog_branding(second, storage_root=storage_root)
        assert a.source_profile_id != b.source_profile_id
        assert hash_resolved_catalog_branding(a) == hash_resolved_catalog_branding(b)
        changed = a.model_copy(update={"social_handle": "@qa"})
        assert hash_resolved_catalog_branding(changed) != hash_resolved_catalog_branding(a)
        asset = ingest_catalog_brand_logo(session, image_bytes("PNG"), storage_root=storage_root)
        update_catalog_brand_profile(session, first, {}, logo_asset=asset, change_logo=True, storage_root=storage_root)
        with_logo = resolve_catalog_branding(first, storage_root=storage_root)
        assert hash_resolved_catalog_branding(with_logo) != hash_resolved_catalog_branding(a)


def test_synthetic_harness_branding_is_db_free_and_template_has_no_publisher_literal():
    view = build_stress_view_model()
    assert view.branding.display_name == "CATÁLOGO QA - DATOS SINTÉTICOS"
    assert view.branding.logo_data_uri is None
    html = render_catalog_html(view, resolve_catalog_template("grabelan-catalog-v1", template_root=TEMPLATE_ROOT))
    source = (TEMPLATE_ROOT / "catalog-v1.html.jinja").read_text(encoding="utf-8")
    assert "GRABELAN" not in source.upper()
    assert "CATÁLOGO QA - DATOS SINTÉTICOS" in html
    assert "--brand-primary: #87663F" in html
    assert 'class="product-row"' in html
    assert "http://" not in html and "https://" not in html


def test_developer_cli_requires_explicit_brand_and_supports_profile_commands():
    brand = brand_profile_parser()
    assert brand.parse_args(["create", "--key", "grabelan", "--display-name", "Grabelan", "--primary-color", "#112233", "--accent-color", "#445566"]).command == "create"
    assert brand.parse_args(["update", "--key", "grabelan", "--clear-logo"]).clear_logo
    assert brand.parse_args(["list"]).command == "list"
    assert brand.parse_args(["deactivate", "--key", "grabelan"]).command == "deactivate"
    render = catalog_render_parser()
    with pytest.raises(SystemExit):
        render.parse_args(["--snapshot-id", str(uuid.uuid4())])
    render_args = render.parse_args(["--snapshot-id", str(uuid.uuid4()), "--brand-key", "grabelan"])
    assert render_args.brand_key == "grabelan"
    assert render_args.layout == "classic"


def test_frozen_branding_schema_rejects_unsupported_version_and_machine_path():
    with pytest.raises(ValidationError):
        ResolvedCatalogBranding(
            schema_version="catalog-branding-v2", source_profile_id=uuid.uuid4(),
            profile_key="grabelan", display_name="Grabelan", primary_color="#112233",
            accent_color="#445566",
        )
    with pytest.raises(ValidationError):
        FrozenCatalogBrandLogo(
            source_brand_asset_id=uuid.uuid4(), checksum_sha256="a" * 64,
            mime_type="image/png", file_size_bytes=100, width=10, height=10,
            storage_relative_path="C:/Users/salomon/logo.png",
        )
