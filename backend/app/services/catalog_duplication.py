"""Project frozen selection into a current, non-persisted Builder draft."""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import CatalogBrandProfile, CatalogBuild, CatalogCoverAsset, Product
from app.domain.catalog_duplication import (
    CatalogDuplicateTemplate, DuplicateClosing, DuplicateCover, DuplicateHero,
    DuplicateNotice, DuplicatePalette, DuplicateProduct, DuplicatePublisher,
    DuplicateSource, DuplicateVersionChoice,
)
from app.domain.schemas import (
    CatalogClosingContactChoice, CatalogClosingCreate, CatalogClosingQrCreate,
    CatalogRenderJobPayloadV3, CatalogRenderJobPayloadV4, CatalogRenderJobPayloadV5,
)
from app.rendering.catalog_closings import UnknownCatalogClosingError, resolve_catalog_closing_definition
from app.rendering.catalog_covers import UnknownCatalogCoverError, resolve_catalog_cover_definition
from app.rendering.catalog_layouts import UnknownCatalogLayoutError, resolve_catalog_layout
from app.rendering.catalog_themes import UnknownCatalogThemeError, resolve_catalog_theme_definition
from app.services.catalog_builder import get_catalog_builder_product_summary
from app.services.catalog_cover_assets import (
    DEFAULT_STORAGE_ROOT, CatalogCoverAssetIntegrityError, freeze_catalog_cover_asset,
)
from app.services.catalog_history import _payload, _snapshot


class UnknownDuplicateSourceError(ValueError):
    pass


def _notice(code: str, field: str, product_id: uuid.UUID | None = None) -> DuplicateNotice:
    return DuplicateNotice(code=code, field=field, product_id=product_id)


def get_catalog_duplicate_template(
    session: Session, build_id: uuid.UUID, *, storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> CatalogDuplicateTemplate:
    """No writes: historical IDs choose candidates; all readiness is current."""
    build = session.get(CatalogBuild, build_id)
    if build is None:
        raise UnknownDuplicateSourceError("Catalog not found")
    payload = _payload(build.job)
    snapshot = _snapshot(session, build)
    if payload is not None and (
        build.catalog_snapshot is None
        or payload.catalog_snapshot_id != build.catalog_snapshot_id
        or payload.snapshot_content_hash.lower() != build.catalog_snapshot.content_hash.lower()
    ):
        payload = None
    source = DuplicateSource(
        build_id=build.id, created_at=build.created_at, status=build.job.status.value,
        render_version=build.job.job_type,
        historical_publisher_name=payload.branding_data.display_name if payload else None,
        historical_product_count=sum(len(section.products) for section in snapshot.sections) if snapshot else None,
    )
    if payload is None or snapshot is None:
        return CatalogDuplicateTemplate(
            source=source, can_initialize=False,
            unavailable_reason="unsupported_configuration" if payload is None else "invalid_snapshot",
        )

    warnings: list[DuplicateNotice] = []
    defaults: list[DuplicateNotice] = []
    provenance = payload.builder_choice_provenance
    if snapshot.currency != "PYG":
        warnings.append(_notice("source_currency_defaulted", "currency"))
    products: list[DuplicateProduct] = []
    for section in snapshot.sections:
        for frozen in section.products:
            current = session.get(Product, frozen.source_product_id)
            if current is None:
                products.append(DuplicateProduct(
                    product_id=frozen.source_product_id, status="unavailable",
                    historical_name=frozen.product_name, historical_brand_name=frozen.brand_name,
                ))
                warnings.append(_notice("product_unavailable", "products", frozen.source_product_id))
                continue
            summary = get_catalog_builder_product_summary(
                session, product_id=current.id, currency="PYG",
            )
            state = "ready" if summary.readiness.ready else "not_ready"
            products.append(DuplicateProduct(
                product_id=current.id, status=state,
                historical_name=frozen.product_name, historical_brand_name=frozen.brand_name,
                current_name=summary.product_name, current_brand_name=summary.brand_name,
                blockers=summary.readiness.blockers if state == "not_ready" else [],
            ))
            if state == "not_ready":
                warnings.append(_notice("product_not_ready", "products", current.id))
    if not any(item.status == "ready" for item in products):
        warnings.append(_notice("no_ready_products", "products"))

    historical_publisher = payload.branding_data
    profile = session.get(CatalogBrandProfile, payload.catalog_brand_profile_id)
    if profile is not None and profile.is_active:
        publisher = DuplicatePublisher(
            state="selected", historical_name=historical_publisher.display_name,
            current_profile_id=profile.id, current_name=profile.display_name,
        )
    else:
        publisher = DuplicatePublisher(state="unavailable", historical_name=historical_publisher.display_name)
        warnings.append(_notice("publisher_unavailable", "publisher"))

    try:
        registered = resolve_catalog_layout(payload.layout_key)
        if registered.version != payload.layout_version:
            raise UnknownCatalogLayoutError("version unavailable")
        layout = DuplicateVersionChoice(state="copied", key=registered.key, version=registered.version)
    except UnknownCatalogLayoutError:
        layout = DuplicateVersionChoice(state="unavailable", key=payload.layout_key, version=payload.layout_version)
        warnings.append(_notice("layout_version_unavailable", "layout"))

    if isinstance(payload, CatalogRenderJobPayloadV3):
        frozen_theme = payload.theme_data
        try:
            resolve_catalog_theme_definition(frozen_theme.theme_key, frozen_theme.theme_version)
            theme = DuplicateVersionChoice(state="copied", key=frozen_theme.theme_key, version=frozen_theme.theme_version)
        except UnknownCatalogThemeError:
            theme = DuplicateVersionChoice(state="unavailable", key=frozen_theme.theme_key, version=frozen_theme.theme_version)
            warnings.append(_notice("theme_version_unavailable", "theme"))
        if frozen_theme.palette_source == "publisher":
            palette = DuplicatePalette(state="copied", source="publisher")
        elif provenance is not None and (provenance.primary_color_override is not None or provenance.accent_color_override is not None):
            palette = DuplicatePalette(
                state="copied", source="custom",
                primary_color_override=provenance.primary_color_override,
                accent_color_override=provenance.accent_color_override,
            )
        else:
            # The resolved pair does not say which of the two channels was an
            # explicit override. Never turn a publisher-derived channel into
            # an invented custom override.
            palette = DuplicatePalette(
                state="unresolved", source="unresolved",
                historical_resolved_primary=frozen_theme.primary_color,
                historical_resolved_accent=frozen_theme.accent_color,
            )
            warnings.append(_notice("palette_override_provenance_unavailable", "palette"))
    else:
        theme = DuplicateVersionChoice(state="defaulted", key="minimal", version="1")
        palette = DuplicatePalette(state="copied", source="publisher")
        defaults.append(_notice("source_predates_theme", "theme"))

    cover = DuplicateCover(enabled=False, state="defaulted")
    if isinstance(payload, CatalogRenderJobPayloadV4):
        frozen_cover = payload.cover_data
        cover = DuplicateCover(enabled=frozen_cover.enabled, state="copied")
        if frozen_cover.enabled:
            cover.key, cover.version = frozen_cover.cover_key, frozen_cover.cover_version
            cover.title, cover.subtitle, cover.edition_label = frozen_cover.title, frozen_cover.subtitle, frozen_cover.edition_label
            cover.show_publisher_logo = (
                provenance.cover_show_publisher_logo
                if provenance is not None and provenance.cover_show_publisher_logo is not None
                else frozen_cover.show_publisher_logo
            )
            if provenance is None and historical_publisher.logo is None:
                warnings.append(_notice("logo_choice_provenance_unavailable", "cover.show_publisher_logo"))
            try:
                resolve_catalog_cover_definition(frozen_cover.cover_key or "", frozen_cover.cover_version or "")
            except UnknownCatalogCoverError:
                cover.state = "unavailable"
                warnings.append(_notice("cover_version_unavailable", "cover"))
            if frozen_cover.hero is not None:
                asset = session.get(CatalogCoverAsset, frozen_cover.hero.source_cover_asset_id)
                try:
                    if asset is None or freeze_catalog_cover_asset(asset, storage_root=storage_root) != frozen_cover.hero:
                        raise CatalogCoverAssetIntegrityError("historical Hero cannot be reused")
                    cover.hero = DuplicateHero(
                        asset_id=asset.id, mime_type=asset.mime_type,
                        width=asset.width, height=asset.height,
                        preview_url=f"/api/catalog-builder/cover-assets/{asset.id}/image",
                    )
                except CatalogCoverAssetIntegrityError:
                    cover.hero_unavailable = True
                    warnings.append(_notice("hero_asset_unavailable", "cover.hero"))
    else:
        defaults.append(_notice("source_predates_cover", "cover"))

    closing = DuplicateClosing()
    if isinstance(payload, CatalogRenderJobPayloadV5):
        frozen_closing = payload.closing_data
        if not frozen_closing.enabled:
            closing = DuplicateClosing(state="copied", choice=CatalogClosingCreate(enabled=False))
        else:
            closing_state = "copied"
            try:
                resolve_catalog_closing_definition(frozen_closing.closing_key or "", frozen_closing.closing_version or "")
            except UnknownCatalogClosingError:
                closing_state = "unavailable"
                warnings.append(_notice("closing_version_unavailable", "closing"))
            contacts = {item.kind: item.value for item in frozen_closing.contacts}
            choices = {}
            for kind in ("publisher_contact", "publisher_social", "whatsapp", "phone", "instagram", "website", "address"):
                if kind not in contacts:
                    choices[kind] = CatalogClosingContactChoice(enabled=False)
                elif kind in ("publisher_contact", "publisher_social"):
                    override = (provenance.publisher_contact_override if kind == "publisher_contact"
                                else provenance.publisher_social_override) if provenance else None
                    choices[kind] = CatalogClosingContactChoice(enabled=True, override=override)
                    if provenance is None:
                        # An old resolved contact cannot reveal whether an
                        # override was supplied. Use today's profile visibly.
                        warnings.append(_notice("publisher_contact_provenance_unavailable", f"closing.{kind}"))
                    current_value = (profile.contact_text if kind == "publisher_contact" else profile.social_handle) if profile and profile.is_active else None
                    if not override and not current_value:
                        warnings.append(_notice("publisher_contact_unavailable", f"closing.{kind}"))
                else:
                    # These contact kinds always required an explicit Builder
                    # override in the v5 resolver.
                    choices[kind] = CatalogClosingContactChoice(enabled=True, override=contacts[kind])
            qr = CatalogClosingQrCreate(
                enabled=frozen_closing.qr_enabled,
                target_type=frozen_closing.qr_target_type if frozen_closing.qr_enabled else None,
                custom_url=(frozen_closing.qr_target_url if frozen_closing.qr_target_type == "custom_url" else None),
            )
            if provenance is None and historical_publisher.logo is None:
                warnings.append(_notice("logo_choice_provenance_unavailable", "closing.show_publisher_logo"))
            closing = DuplicateClosing(state=closing_state, choice=CatalogClosingCreate(
                enabled=True, closing_key=frozen_closing.closing_key,
                closing_version=frozen_closing.closing_version,
                heading=frozen_closing.heading, note=frozen_closing.note,
                show_publisher_logo=(provenance.closing_show_publisher_logo
                                     if provenance is not None and provenance.closing_show_publisher_logo is not None
                                     else frozen_closing.show_publisher_logo),
                qr=qr, **choices,
            ))
    else:
        defaults.append(_notice("source_predates_closing", "closing"))

    return CatalogDuplicateTemplate(
        source=source, can_initialize=True, products=products,
        publisher=publisher, layout=layout, theme=theme, palette=palette,
        cover=cover, closing=closing, defaults_applied=defaults, warnings=warnings,
    )
