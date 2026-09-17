"""Developer-only current catalog publisher configuration tooling."""

import argparse
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import CatalogBrandProfile
from app.db.session import DATABASE_URL, create_sqlite_engine
from app.services.catalog_branding import (
    CatalogBrandingError,
    create_catalog_brand_profile,
    ingest_catalog_brand_logo,
    update_catalog_brand_profile,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local catalog publisher profiles.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--key", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--primary-color", required=True)
    create.add_argument("--accent-color", required=True)
    create.add_argument("--contact-text")
    create.add_argument("--social-handle")
    create.add_argument("--logo-path", type=Path)
    update = commands.add_parser("update")
    update.add_argument("--key", required=True)
    update.add_argument("--display-name")
    update.add_argument("--primary-color")
    update.add_argument("--accent-color")
    update.add_argument("--contact-text")
    update.add_argument("--social-handle")
    update.add_argument("--logo-path", type=Path)
    update.add_argument("--clear-logo", action="store_true")
    update.add_argument("--clear-contact", action="store_true")
    update.add_argument("--clear-social", action="store_true")
    commands.add_parser("list")
    deactivate = commands.add_parser("deactivate")
    deactivate.add_argument("--key", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    engine = create_sqlite_engine(os.environ.get("DATABASE_URL", DATABASE_URL))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            if args.command == "list":
                profiles = list(session.scalars(select(CatalogBrandProfile).order_by(CatalogBrandProfile.key)))
                print(json.dumps([_summary(profile) for profile in profiles], indent=2, sort_keys=True))
                return
            if args.command == "create":
                logo = _ingest_optional_logo(session, args.logo_path)
                profile = create_catalog_brand_profile(session, {
                    "key": args.key, "display_name": args.display_name,
                    "primary_color": args.primary_color, "accent_color": args.accent_color,
                    "contact_text": args.contact_text, "social_handle": args.social_handle,
                }, logo_asset=logo)
            else:
                profile = session.scalar(select(CatalogBrandProfile).where(CatalogBrandProfile.key == args.key))
                if profile is None:
                    raise CatalogBrandingError("catalog brand profile not found")
                if args.command == "deactivate":
                    update_catalog_brand_profile(session, profile, {"is_active": False})
                else:
                    if args.logo_path and args.clear_logo:
                        raise CatalogBrandingError("choose logo replacement or clear-logo, not both")
                    changes = {}
                    for arg_name, field_name in (
                        ("display_name", "display_name"), ("primary_color", "primary_color"),
                        ("accent_color", "accent_color"), ("contact_text", "contact_text"),
                        ("social_handle", "social_handle"),
                    ):
                        value = getattr(args, arg_name)
                        if value is not None:
                            changes[field_name] = value
                    if args.clear_contact:
                        changes["contact_text"] = None
                    if args.clear_social:
                        changes["social_handle"] = None
                    logo = _ingest_optional_logo(session, args.logo_path)
                    update_catalog_brand_profile(
                        session, profile, changes, logo_asset=logo,
                        change_logo=bool(args.logo_path or args.clear_logo),
                    )
            session.commit()
            print(json.dumps(_summary(profile), indent=2, sort_keys=True))
    except IntegrityError:
        raise SystemExit("profile key already exists or catalog branding integrity failed") from None
    except (CatalogBrandingError, OSError) as error:
        raise SystemExit(str(error)) from None
    finally:
        engine.dispose()


def _ingest_optional_logo(session, path: Path | None):
    if path is None:
        return None
    return ingest_catalog_brand_logo(session, path.read_bytes())


def _summary(profile: CatalogBrandProfile) -> dict[str, str | bool | None]:
    return {
        "id": str(profile.id), "key": profile.key, "display_name": profile.display_name,
        "primary_color": profile.primary_color, "accent_color": profile.accent_color,
        "logo_asset_id": str(profile.logo_asset_id) if profile.logo_asset_id else None,
        "contact_text": profile.contact_text, "social_handle": profile.social_handle,
        "is_active": profile.is_active,
    }


if __name__ == "__main__":
    main()
