import hashlib
import uuid
from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.db.session import create_sqlite_engine
from app.domain.schemas import CatalogCoverCreate, FrozenCatalogBrandLogo, ResolvedCatalogBranding
from app.rendering.catalog_covers import (
    UnknownCatalogCoverError, catalog_cover_definitions, hash_resolved_catalog_cover,
    resolve_catalog_cover, resolve_catalog_cover_definition,
)
from app.services.catalog_cover_assets import (
    CatalogCoverAssetError, CatalogCoverAssetIntegrityError, CatalogCoverAssetTooLargeError,
    ingest_catalog_cover_asset, freeze_catalog_cover_asset, load_frozen_catalog_cover_hero,
)


def _image(fmt: str = "PNG", size: tuple[int, int] = (30, 40)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (200, 180, 145)).save(output, format=fmt)
    return output.getvalue()


def _branding() -> ResolvedCatalogBranding:
    return ResolvedCatalogBranding(
        schema_version="catalog-branding-v1", source_profile_id=uuid.uuid4(),
        profile_key="publisher", display_name="Example Publisher",
        primary_color="#123456", accent_color="#AABBCC",
    )


@pytest.fixture
def cover_store(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'covers.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory, tmp_path / "storage"
    engine.dispose()


def test_cover_registry_and_plain_text_contract():
    definitions = catalog_cover_definitions()
    assert [(item.key, item.version, item.requires_hero) for item in definitions] == [
        ("minimal", "1", False), ("editorial", "1", False), ("hero", "1", True),
    ]
    assert len({(item.key, item.version) for item in definitions}) == 3
    for key, version in (("unknown", "1"), ("hero", "2")):
        with pytest.raises(UnknownCatalogCoverError):
            resolve_catalog_cover_definition(key, version)
    choice = CatalogCoverCreate(enabled=True, cover_key="editorial", cover_version="1",
        title="  Catálogo\n  Mayorista  ", subtitle="  Línea   nueva ", edition_label="  2026  ")
    assert (choice.title, choice.subtitle, choice.edition_label) == ("Catálogo Mayorista", "Línea nueva", "2026")
    for values in (
        {"enabled": True, "cover_key": "minimal", "cover_version": "1", "title": " "},
        {"enabled": True, "cover_key": "minimal", "cover_version": "1", "title": "x" * 81},
        {"enabled": True, "cover_key": "minimal", "cover_version": "1", "title": "Ok", "subtitle": "x" * 181},
        {"enabled": True, "cover_key": "minimal", "cover_version": "1", "title": "Ok", "edition_label": "x" * 61},
        {"enabled": True, "cover_key": "minimal", "cover_version": "1", "title": "<script>"},
        {"enabled": True, "cover_key": "hero", "cover_version": "1", "title": "Ok"},
        {"enabled": False, "title": "ignored"},
    ):
        with pytest.raises(ValidationError):
            CatalogCoverCreate.model_validate(values)


def test_cover_asset_formats_deduplication_and_integrity(cover_store):
    factory, root = cover_store
    with factory() as session:
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            data = _image(fmt)
            asset = ingest_catalog_cover_asset(session, data, declared_mime_type=mime, storage_root=root)
            same = ingest_catalog_cover_asset(session, data, declared_mime_type=mime, storage_root=root)
            assert same.id == asset.id
            frozen = freeze_catalog_cover_asset(asset, storage_root=root)
            assert (frozen.width, frozen.height, frozen.mime_type) == (30, 40, mime)
            assert load_frozen_catalog_cover_hero(frozen, storage_root=root) == data
            assert str(root.resolve()) not in frozen.storage_relative_path
            path = root / frozen.storage_relative_path
            assert path.name.startswith(hashlib.sha256(data).hexdigest())
            path.write_bytes(b"tampered")
            with pytest.raises(CatalogCoverAssetIntegrityError):
                load_frozen_catalog_cover_hero(frozen, storage_root=root)
    with factory() as session:
        with pytest.raises(CatalogCoverAssetError):
            ingest_catalog_cover_asset(session, b"invalid", declared_mime_type="image/png", storage_root=root)
        with pytest.raises(CatalogCoverAssetError):
            ingest_catalog_cover_asset(session, _image(), declared_mime_type="image/jpeg", storage_root=root)
        with pytest.raises(CatalogCoverAssetTooLargeError):
            ingest_catalog_cover_asset(session, b"x" * (10 * 1024 * 1024 + 1), declared_mime_type="image/png", storage_root=root)


def test_cover_resolution_hash_and_disabled_canonical_form(cover_store):
    factory, root = cover_store
    branding = _branding()
    with factory() as session:
        disabled = resolve_catalog_cover(CatalogCoverCreate(enabled=False), branding, session, storage_root=root)
        assert disabled.model_dump(exclude_none=True) == {"schema_version": "catalog-cover-v1", "enabled": False, "show_publisher_logo": False}
        minimal = resolve_catalog_cover(CatalogCoverCreate(enabled=True, cover_key="minimal", cover_version="1", title="Catálogo"), branding, session, storage_root=root)
        same = resolve_catalog_cover(CatalogCoverCreate(enabled=True, cover_key="minimal", cover_version="1", title="Catálogo"), _branding(), session, storage_root=root)
        assert not minimal.show_publisher_logo
        assert hash_resolved_catalog_cover(minimal) == hash_resolved_catalog_cover(same)
        variants = [
            CatalogCoverCreate(enabled=True, cover_key="editorial", cover_version="1", title="Catálogo"),
            CatalogCoverCreate(enabled=True, cover_key="minimal", cover_version="1", title="Otro"),
            CatalogCoverCreate(enabled=True, cover_key="minimal", cover_version="1", title="Catálogo", subtitle="Sub"),
            CatalogCoverCreate(enabled=True, cover_key="minimal", cover_version="1", title="Catálogo", edition_label="2026"),
        ]
        hashes = {hash_resolved_catalog_cover(resolve_catalog_cover(item, branding, session, storage_root=root)) for item in variants}
        assert len(hashes | {hash_resolved_catalog_cover(minimal), hash_resolved_catalog_cover(disabled)}) == 6
        logo = FrozenCatalogBrandLogo(
            source_brand_asset_id=uuid.uuid4(), checksum_sha256="a" * 64,
            mime_type="image/png", file_size_bytes=1, width=1, height=1,
            storage_relative_path=f"branding/aa/{'a' * 64}.png",
        )
        with_logo = branding.model_copy(update={"logo": logo})
        visible = resolve_catalog_cover(CatalogCoverCreate(enabled=True, cover_key="minimal", cover_version="1", title="Catálogo", show_publisher_logo=True), with_logo, session, storage_root=root)
        hidden = resolve_catalog_cover(CatalogCoverCreate(enabled=True, cover_key="minimal", cover_version="1", title="Catálogo", show_publisher_logo=False), with_logo, session, storage_root=root)
        assert visible.show_publisher_logo and not hidden.show_publisher_logo
        assert hash_resolved_catalog_cover(visible) != hash_resolved_catalog_cover(hidden)
        first = ingest_catalog_cover_asset(session, _image(), declared_mime_type="image/png", storage_root=root)
        second = ingest_catalog_cover_asset(session, _image(size=(31, 40)), declared_mime_type="image/png", storage_root=root)
        hero_choice = dict(enabled=True, cover_key="hero", cover_version="1", title="Catálogo")
        a = resolve_catalog_cover(CatalogCoverCreate(**hero_choice, hero_asset_id=first.id), branding, session, storage_root=root)
        b = resolve_catalog_cover(CatalogCoverCreate(**hero_choice, hero_asset_id=second.id), branding, session, storage_root=root)
        assert hash_resolved_catalog_cover(a) != hash_resolved_catalog_cover(b)
