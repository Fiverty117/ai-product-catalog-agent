import hashlib
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Brand,
    DerivedImage,
    DerivedImageReview,
    Photo,
    PhotoPresentationPreference,
    Price,
    Product,
    SKU,
)
from app.db.session import create_sqlite_engine
from app.domain.enums import (
    CatalogReadinessIssueCode,
    DerivedImageReviewDecision,
    DerivedImageReviewState,
    ExtractionRunStatus,
    PhotoPresentationAssetType,
    PhotoPresentationWarning,
    PhotoRole,
)
from app.domain.schemas import DerivedImageReviewCreate, ImageEnhancementJobPayload
from app.services.catalog_readiness import evaluate_product_catalog_readiness
from app.services.categories import assign_product_category, create_category
from app.services.image_enhancement import (
    complete_image_enhancement_run,
    create_running_image_enhancement_run,
    mark_image_enhancement_run_failed,
    store_processed_image,
)
from app.services.image_presentation import (
    DerivedImageAssetIntegrityError,
    InvalidDerivedImageLineageError,
    UnknownDerivedImageError,
    UnapprovedDerivedImageError,
    create_derived_image_review,
    get_current_derived_image_review,
    get_derived_image_review_state,
    resolve_effective_photo_presentation,
    select_derived_image_for_photo,
    use_original_photo_presentation,
)
from app.services.photo_intake import register_original_photo

AS_OF = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'presentation.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.info["originals"] = tmp_path / "storage" / "originals"
        session.info["processed"] = tmp_path / "storage" / "processed"
        yield session
    engine.dispose()


def image_bytes(color=(10, 20, 30), image_format="PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (13, 11), color).save(output, format=image_format)
    return output.getvalue()


def make_photo(session: Session, *, product_owned=False) -> Photo:
    suffix = uuid.uuid4().hex
    product = Product(name=f"Product {suffix}", brand=Brand(name=f"Brand {suffix}"))
    sku = SKU(product=product)
    session.add(sku)
    session.flush()
    photo = register_original_photo(
        session,
        image_bytes=image_bytes(),
        original_filename="front.png",
        originals_dir=session.info["originals"],
        sku_id=None if product_owned else sku.id,
        role=PhotoRole.FRONT,
    )
    if product_owned:
        photo.product = product
    session.flush()
    return photo


def make_derived(
    session: Session,
    photo: Photo,
    *,
    color=(80, 90, 100),
) -> DerivedImage:
    run = create_running_image_enhancement_run(
        session, payload=enhancement_payload(photo)
    )
    stored = store_processed_image(
        image_bytes(color), processed_dir=session.info["processed"]
    )
    return complete_image_enhancement_run(session, run, stored_image=stored)


def enhancement_payload(photo: Photo) -> ImageEnhancementJobPayload:
    return ImageEnhancementJobPayload(
        source_photo_id=photo.id,
        source_checksum_sha256=photo.checksum_sha256,
        provider="openai",
        model="gpt-image-2.5-sunburst",
        prompt_version="product-image-enhancement-v1",
        config_version="image-enhancement-config-v1",
        parameters={
            "quality": "high",
            "output_format": "png",
            "size": "auto",
            "background": "auto",
        },
    )


def review(
    session: Session,
    derived: DerivedImage,
    decision: DerivedImageReviewDecision,
) -> DerivedImageReview:
    return create_derived_image_review(
        session,
        DerivedImageReviewCreate(
            derived_image_id=derived.id,
            decision=decision,
        ),
    )


def approve(session: Session, derived: DerivedImage) -> DerivedImageReview:
    return review(session, derived, DerivedImageReviewDecision.APPROVED)


def test_review_history_derives_current_state_without_rewriting_rows(
    session: Session,
) -> None:
    photo = make_photo(session)
    derived = make_derived(session, photo)
    assert get_derived_image_review_state(
        session, derived.id
    ) is DerivedImageReviewState.UNREVIEWED

    first = approve(session, derived)
    first_state = (first.id, first.decision, first.created_at)
    assert get_derived_image_review_state(
        session, derived.id
    ) is DerivedImageReviewState.APPROVED

    second = review(session, derived, DerivedImageReviewDecision.REJECTED)
    assert get_derived_image_review_state(
        session, derived.id
    ) is DerivedImageReviewState.REJECTED
    third = approve(session, derived)

    assert get_current_derived_image_review(session, derived.id).id == third.id
    assert get_derived_image_review_state(
        session, derived.id
    ) is DerivedImageReviewState.APPROVED
    assert (first.id, first.decision, first.created_at) == first_state
    assert [row.decision for row in derived.reviews] == [
        DerivedImageReviewDecision.APPROVED,
        DerivedImageReviewDecision.REJECTED,
        DerivedImageReviewDecision.APPROVED,
    ]
    assert len({first.id, second.id, third.id}) == 3

    with pytest.raises(ValidationError):
        DerivedImageReviewCreate.model_validate(
            {
                "derived_image_id": derived.id,
                "decision": "approved",
                "unexpected": True,
            }
        )


def test_latest_review_uses_created_at_then_uuid(session: Session) -> None:
    derived = make_derived(session, make_photo(session))
    timestamp = datetime(2026, 9, 19, tzinfo=timezone.utc)
    lower = DerivedImageReview(
        id=uuid.UUID(int=1),
        derived_image=derived,
        decision=DerivedImageReviewDecision.APPROVED,
        created_at=timestamp,
    )
    higher = DerivedImageReview(
        id=uuid.UUID(int=2),
        derived_image=derived,
        decision=DerivedImageReviewDecision.REJECTED,
        created_at=timestamp,
    )
    session.add_all([lower, higher])
    session.flush()

    assert get_current_derived_image_review(session, derived.id).id == higher.id
    assert get_derived_image_review_state(
        session, derived.id
    ) is DerivedImageReviewState.REJECTED


def test_unknown_and_failed_enhancement_output_cannot_be_reviewed(
    session: Session,
) -> None:
    with pytest.raises(UnknownDerivedImageError):
        create_derived_image_review(
            session,
            DerivedImageReviewCreate(
                derived_image_id=uuid.uuid4(),
                decision=DerivedImageReviewDecision.APPROVED,
            ),
        )

    photo = make_photo(session)
    run = create_running_image_enhancement_run(
        session, payload=enhancement_payload(photo)
    )
    assert run.status is ExtractionRunStatus.RUNNING
    mark_image_enhancement_run_failed(session, run, error="failed")
    stored = store_processed_image(
        image_bytes((1, 2, 3)), processed_dir=session.info["processed"]
    )
    invalid = DerivedImage(
        source_photo=photo,
        enhancement_run=run,
        **stored.__dict__,
    )
    session.add(invalid)
    session.flush()

    with pytest.raises(InvalidDerivedImageLineageError, match="succeeded"):
        approve(session, invalid)


@pytest.mark.parametrize("failure", ["missing", "checksum", "corrupt"])
def test_review_requires_intact_derived_asset(session: Session, failure: str) -> None:
    derived = make_derived(session, make_photo(session))
    path = Path(derived.file_path)
    if failure == "missing":
        path.unlink()
    elif failure == "checksum":
        path.write_bytes(image_bytes((2, 3, 4)))
    else:
        content = b"corrupt"
        path.write_bytes(content)
        derived.checksum_sha256 = hashlib.sha256(content).hexdigest()
        derived.file_size_bytes = len(content)
        session.flush()

    with pytest.raises(DerivedImageAssetIntegrityError):
        approve(session, derived)
    assert session.scalar(select(func.count()).select_from(DerivedImageReview)) == 0


def test_review_never_changes_original_photo(session: Session) -> None:
    photo = make_photo(session)
    derived = make_derived(session, photo)
    original = (
        photo.file_path,
        photo.checksum_sha256,
        photo.width,
        photo.height,
        photo.product_id,
        photo.sku_id,
        Path(photo.file_path).read_bytes(),
    )

    approve(session, derived)

    assert (
        photo.file_path,
        photo.checksum_sha256,
        photo.width,
        photo.height,
        photo.product_id,
        photo.sku_id,
        Path(photo.file_path).read_bytes(),
    ) == original


def test_only_approved_same_photo_derived_image_can_be_selected(
    session: Session,
) -> None:
    photo = make_photo(session)
    unreviewed = make_derived(session, photo)
    rejected = make_derived(session, photo, color=(20, 30, 40))
    other = make_derived(session, make_photo(session), color=(30, 40, 50))
    review(session, rejected, DerivedImageReviewDecision.REJECTED)
    approve(session, other)

    with pytest.raises(UnapprovedDerivedImageError):
        select_derived_image_for_photo(
            session, photo_id=photo.id, derived_image_id=unreviewed.id
        )
    with pytest.raises(UnapprovedDerivedImageError):
        select_derived_image_for_photo(
            session, photo_id=photo.id, derived_image_id=rejected.id
        )
    with pytest.raises(InvalidDerivedImageLineageError):
        select_derived_image_for_photo(
            session, photo_id=photo.id, derived_image_id=other.id
        )


def test_selection_is_singular_idempotent_and_preserves_reviews(
    session: Session,
) -> None:
    photo = make_photo(session)
    first = make_derived(session, photo)
    second = make_derived(session, photo, color=(40, 50, 60))
    first_review = approve(session, first)
    second_review = approve(session, second)

    preference = select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=first.id
    )
    same = select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=first.id
    )
    replacement = select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=second.id
    )

    assert preference.id == same.id == replacement.id
    assert replacement.selected_derived_image_id == second.id
    assert session.scalar(
        select(func.count()).select_from(PhotoPresentationPreference)
    ) == 1
    assert not hasattr(replacement, "product_id")
    assert not hasattr(replacement, "sku_id")
    assert get_current_derived_image_review(session, first.id).id == first_review.id
    assert get_current_derived_image_review(session, second.id).id == second_review.id


def test_revert_uses_original_without_changing_approval_or_files(
    session: Session,
) -> None:
    photo = make_photo(session)
    derived = make_derived(session, photo)
    approve(session, derived)
    path = Path(derived.file_path)
    select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )

    preference = use_original_photo_presentation(session, photo_id=photo.id)
    effective = resolve_effective_photo_presentation(session, photo_id=photo.id)

    assert preference.selected_derived_image_id is None
    assert get_derived_image_review_state(
        session, derived.id
    ) is DerivedImageReviewState.APPROVED
    assert path.is_file()
    assert effective.asset_type is PhotoPresentationAssetType.ORIGINAL


def test_rejecting_selected_image_atomically_reverts_without_alternate(
    session: Session,
) -> None:
    photo = make_photo(session)
    selected = make_derived(session, photo)
    alternate = make_derived(session, photo, color=(50, 60, 70))
    approve(session, selected)
    approve(session, alternate)
    preference = select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=selected.id
    )

    review(session, selected, DerivedImageReviewDecision.REJECTED)

    assert preference.selected_derived_image_id is None
    assert get_derived_image_review_state(
        session, selected.id
    ) is DerivedImageReviewState.REJECTED
    assert get_derived_image_review_state(
        session, alternate.id
    ) is DerivedImageReviewState.APPROVED
    assert Path(selected.file_path).is_file()
    assert selected.enhancement_run is not None
    assert resolve_effective_photo_presentation(
        session, photo_id=photo.id
    ).asset_type is PhotoPresentationAssetType.ORIGINAL


def test_rejection_and_preference_revert_roll_back_together(
    session: Session, monkeypatch
) -> None:
    photo = make_photo(session)
    derived = make_derived(session, photo)
    approval = approve(session, derived)
    preference = select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )
    session.commit()
    original_flush = session.flush

    def fail_flush(*args, **kwargs):
        raise RuntimeError("forced transaction failure")

    monkeypatch.setattr(session, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="forced"):
        review(session, derived, DerivedImageReviewDecision.REJECTED)
    session.rollback()
    monkeypatch.setattr(session, "flush", original_flush)

    persisted_preference = session.get(PhotoPresentationPreference, preference.id)
    assert persisted_preference.selected_derived_image_id == derived.id
    assert get_current_derived_image_review(session, derived.id).id == approval.id


def test_effective_resolver_requires_explicit_selection_and_is_read_only(
    session: Session,
) -> None:
    photo = make_photo(session)
    first = make_derived(session, photo)
    second = make_derived(session, photo, color=(60, 70, 80))
    approve(session, first)
    approve(session, second)

    original = resolve_effective_photo_presentation(session, photo_id=photo.id)
    assert original.asset_type is PhotoPresentationAssetType.ORIGINAL
    assert original.derived_image_id is None

    preference = select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=second.id
    )
    session.flush()
    before = (preference.selected_derived_image_id, preference.updated_at)
    effective = resolve_effective_photo_presentation(session, photo_id=photo.id)

    assert effective.asset_type is PhotoPresentationAssetType.DERIVED
    assert effective.derived_image_id == second.id
    assert effective.source_photo_id == photo.id
    assert (preference.selected_derived_image_id, preference.updated_at) == before


def test_review_and_selection_leave_transaction_ownership_to_caller(
    session: Session,
) -> None:
    photo = make_photo(session)
    derived = make_derived(session, photo)
    session.commit()

    approve(session, derived)
    session.rollback()
    assert get_derived_image_review_state(
        session, derived.id
    ) is DerivedImageReviewState.UNREVIEWED

    approve(session, derived)
    session.commit()
    select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )
    session.rollback()
    assert session.scalar(
        select(PhotoPresentationPreference).where(
            PhotoPresentationPreference.photo_id == photo.id
        )
    ) is None


def test_resolver_defensively_ignores_cross_photo_preference(
    session: Session,
) -> None:
    photo = make_photo(session)
    other = make_derived(session, make_photo(session))
    approve(session, other)
    inconsistent = PhotoPresentationPreference(
        photo=photo,
        selected_derived_image=other,
    )
    session.add(inconsistent)
    session.flush()

    effective = resolve_effective_photo_presentation(session, photo_id=photo.id)

    assert effective.asset_type is PhotoPresentationAssetType.ORIGINAL
    assert effective.warnings == [
        PhotoPresentationWarning.PREFERRED_DERIVED_SELECTION_INVALID
    ]
    assert inconsistent.selected_derived_image_id == other.id


def test_missing_selected_asset_falls_back_with_warning_without_repair(
    session: Session,
) -> None:
    photo = make_photo(session)
    derived = make_derived(session, photo)
    approve(session, derived)
    preference = select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )
    Path(derived.file_path).unlink()
    session.flush()

    effective = resolve_effective_photo_presentation(session, photo_id=photo.id)

    assert effective.asset_type is PhotoPresentationAssetType.ORIGINAL
    assert effective.backing_asset_available is True
    assert effective.warnings == [
        PhotoPresentationWarning.PREFERRED_DERIVED_ASSET_MISSING
    ]
    assert preference.selected_derived_image_id == derived.id
    assert not session.dirty


def make_ready_product(session: Session) -> tuple[Product, Photo]:
    photo = make_photo(session, product_owned=True)
    product = photo.product
    sku = product.skus[0]
    category = create_category(session, name=f"Category {uuid.uuid4().hex}")
    assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    session.add(
        Price(
            sku=sku,
            amount="10.0000",
            currency="USD",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source="manual",
            approved=True,
        )
    )
    session.flush()
    return product, photo


def test_readiness_keeps_source_hero_and_resolves_selected_presentation(
    session: Session,
) -> None:
    product, photo = make_ready_product(session)
    unreviewed = make_derived(session, photo)
    approved = make_derived(session, photo, color=(70, 80, 90))

    baseline = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )
    approve(session, approved)
    approved_but_unselected = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )
    select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=approved.id
    )
    selected = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert baseline.hero_photo_id == approved_but_unselected.hero_photo_id == photo.id
    assert baseline.hero_presentation_type is PhotoPresentationAssetType.ORIGINAL
    assert baseline.hero_derived_image_id is None
    assert unreviewed.id != selected.hero_derived_image_id
    assert approved_but_unselected.hero_presentation_type is PhotoPresentationAssetType.ORIGINAL
    assert selected.hero_photo_id == photo.id
    assert selected.hero_presentation_type is PhotoPresentationAssetType.DERIVED
    assert selected.hero_derived_image_id == approved.id


def test_readiness_missing_preferred_asset_warns_but_remains_ready(
    session: Session,
) -> None:
    product, photo = make_ready_product(session)
    derived = make_derived(session, photo)
    approve(session, derived)
    select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )
    Path(derived.file_path).unlink()

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.is_ready is True
    assert report.hero_photo_id == photo.id
    assert report.hero_presentation_type is PhotoPresentationAssetType.ORIGINAL
    assert report.presentation_warnings == [
        PhotoPresentationWarning.PREFERRED_DERIVED_ASSET_MISSING
    ]


def test_derived_presentation_cannot_rescue_missing_source_original(
    session: Session,
) -> None:
    product, photo = make_ready_product(session)
    derived = make_derived(session, photo)
    approve(session, derived)
    select_derived_image_for_photo(
        session, photo_id=photo.id, derived_image_id=derived.id
    )
    Path(photo.file_path).unlink()

    report = evaluate_product_catalog_readiness(
        session, product_id=product.id, currency="USD", as_of=AS_OF
    )

    assert report.is_ready is False
    assert report.hero_photo_id is None
    assert report.hero_presentation_type is None
    assert CatalogReadinessIssueCode.MISSING_CATALOG_PHOTO_ASSET in {
        issue.code for issue in report.blockers
    }
