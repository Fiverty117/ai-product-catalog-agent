import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, Brand, Category, Product, ProductCategory, SKU
from app.db.session import create_sqlite_engine
from app.domain.enums import FieldSource
from app.domain.identity import identity_key_v1
from app.domain.schemas import CategoryCreate, CategoryRead, ProductCategoryRead
from app.services.categories import (
    CategoryAssignmentRoleConflictError,
    DuplicateCategoryIdentityError,
    InactiveCategoryError,
    PrimaryCategoryConflictError,
    UnknownCategoryError,
    UnknownCategoryProductError,
    UnknownProductCategoryAssignmentError,
    assign_product_category,
    create_category,
    list_active_categories,
    remove_product_category,
    set_category_active,
    set_primary_category,
)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'categories.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_product(session: Session, name: str = "Premium Whey") -> Product:
    product = Product(name=name, brand=Brand(name=f"Brand {uuid.uuid4()}"))
    session.add(product)
    session.flush()
    return product


def test_create_category_uses_identity_v1_and_read_schema(session: Session) -> None:
    category = create_category(session, name="  SUPERFOODS  ", sort_order=20)

    assert isinstance(category.id, uuid.UUID)
    assert category.name == "SUPERFOODS"
    assert category.identity_key == "superfoods"
    assert category.sort_order == 20
    assert category.is_active is True
    assert CategoryRead.model_validate(category).identity_key == "superfoods"


@pytest.mark.parametrize("name", ["Superfoods", "SUPERFOODS", " superfoods "])
def test_exact_category_identity_duplicate_is_rejected(
    session: Session, name: str
) -> None:
    existing = create_category(session, name="Superfoods")

    with pytest.raises(DuplicateCategoryIdentityError) as error:
        create_category(session, name=name)

    assert error.value.existing_category is existing
    assert str(existing.id) in str(error.value)


def test_meaningful_space_keeps_categories_distinct(session: Session) -> None:
    compact = create_category(session, name="Superfoods")
    spaced = create_category(session, name="Super Foods")

    assert compact.identity_key != spaced.identity_key


def test_empty_category_and_invalid_sort_order_are_rejected(session: Session) -> None:
    with pytest.raises(ValidationError):
        create_category(session, name=" \t ")
    with pytest.raises(ValidationError):
        create_category(session, name="Valid", sort_order=-1)
    with pytest.raises(ValidationError):
        CategoryCreate(name="Valid", sort_order=True)


def test_category_unicode_identity_matches_identity_key_v1(session: Session) -> None:
    category = create_category(session, name="Ｓｕｐｅｒｆｏｏｄｓ")

    assert category.identity_key == identity_key_v1("Superfoods")


def test_assign_primary_and_secondary_categories_with_human_state(
    session: Session,
) -> None:
    product = make_product(session)
    primary = create_category(session, name="Superfoods")
    secondary = create_category(session, name="Adaptogens")

    primary_assignment = assign_product_category(
        session,
        product_id=product.id,
        category_id=primary.id,
        is_primary=True,
    )
    secondary_assignment = assign_product_category(
        session,
        product_id=product.id,
        category_id=secondary.id,
    )

    assert primary_assignment.is_primary is True
    assert secondary_assignment.is_primary is False
    assert {item.category for item in product.category_assignments} == {
        primary,
        secondary,
    }
    for assignment in (primary_assignment, secondary_assignment):
        assert assignment.source is FieldSource.HUMAN
        assert assignment.verified is True
        assert assignment.locked is True
        assert ProductCategoryRead.model_validate(assignment).product_id == product.id


def test_product_can_have_zero_categories_and_no_skus(session: Session) -> None:
    product = make_product(session)

    assert product.category_assignments == []
    assert product.skus == []

    category = create_category(session, name="Beverages")
    assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    assert session.scalar(select(func.count()).select_from(SKU)) == 0


def test_same_category_can_be_assigned_to_many_products(session: Session) -> None:
    category = create_category(session, name="Beverages")
    first = make_product(session, "Kombucha")
    second = make_product(session, "Sparkling Tea")

    first_assignment = assign_product_category(
        session,
        product_id=first.id,
        category_id=category.id,
        is_primary=True,
    )
    second_assignment = assign_product_category(
        session,
        product_id=second.id,
        category_id=category.id,
        is_primary=True,
    )

    assert first_assignment.category is second_assignment.category is category


def test_same_role_assignment_is_idempotent(session: Session) -> None:
    product = make_product(session)
    category = create_category(session, name="Proteins")

    first = assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )
    second = assign_product_category(
        session,
        product_id=product.id,
        category_id=category.id,
        is_primary=True,
    )

    assert first is second
    assert session.scalar(select(func.count()).select_from(ProductCategory)) == 1


def test_duplicate_association_is_enforced_by_database(session: Session) -> None:
    product = make_product(session)
    category = create_category(session, name="Proteins")
    assign_product_category(
        session, product_id=product.id, category_id=category.id
    )
    session.add(
        ProductCategory(
            product=product,
            category=category,
            source=FieldSource.HUMAN,
            verified=True,
            locked=True,
        )
    )

    with pytest.raises(
        IntegrityError,
        match="product_categories.product_id, product_categories.category_id",
    ):
        session.flush()


def test_second_primary_rejected_by_default(session: Session) -> None:
    product = make_product(session)
    first = create_category(session, name="Superfoods")
    second = create_category(session, name="Adaptogens")
    set_primary_category(
        session, product_id=product.id, category_id=first.id
    )

    with pytest.raises(PrimaryCategoryConflictError, match="replace_primary=true"):
        set_primary_category(
            session, product_id=product.id, category_id=second.id
        )

    assert product.category_assignments[0].category is first
    assert product.category_assignments[0].is_primary is True
    assert session.scalar(select(func.count()).select_from(ProductCategory)) == 1


def test_explicit_primary_replacement_demotes_without_deleting(
    session: Session,
) -> None:
    product = make_product(session)
    first = create_category(session, name="Superfoods")
    second = create_category(session, name="Adaptogens")
    old_assignment = set_primary_category(
        session, product_id=product.id, category_id=first.id
    )
    new_assignment = assign_product_category(
        session, product_id=product.id, category_id=second.id
    )

    promoted = set_primary_category(
        session,
        product_id=product.id,
        category_id=second.id,
        replace_primary=True,
    )

    assert promoted is new_assignment
    assert new_assignment.is_primary is True
    assert old_assignment.is_primary is False
    assert session.get(ProductCategory, old_assignment.id) is old_assignment
    assert session.scalar(select(func.count()).select_from(ProductCategory)) == 2


def test_primary_cannot_be_silently_demoted_by_secondary_assignment(
    session: Session,
) -> None:
    product = make_product(session)
    category = create_category(session, name="Proteins")
    assignment = set_primary_category(
        session, product_id=product.id, category_id=category.id
    )

    with pytest.raises(CategoryAssignmentRoleConflictError):
        assign_product_category(
            session,
            product_id=product.id,
            category_id=category.id,
            is_primary=False,
        )

    assert assignment.is_primary is True


def test_removing_secondary_and_primary_never_auto_promotes(
    session: Session,
) -> None:
    product = make_product(session)
    first = create_category(session, name="Superfoods")
    second = create_category(session, name="Adaptogens")
    primary = set_primary_category(
        session, product_id=product.id, category_id=first.id
    )
    secondary = assign_product_category(
        session, product_id=product.id, category_id=second.id
    )

    remove_product_category(
        session, product_id=product.id, category_id=second.id
    )
    assert session.get(ProductCategory, secondary.id) is None
    assert primary.is_primary is True

    third = create_category(session, name="Roots")
    remaining_secondary = assign_product_category(
        session, product_id=product.id, category_id=third.id
    )
    remove_product_category(
        session, product_id=product.id, category_id=first.id
    )

    assert session.get(ProductCategory, primary.id) is None
    assert remaining_secondary.is_primary is False
    assert session.scalar(
        select(ProductCategory).where(
            ProductCategory.product_id == product.id,
            ProductCategory.is_primary.is_(True),
        )
    ) is None


def test_removing_unknown_assignment_fails_explicitly(session: Session) -> None:
    product = make_product(session)
    category = create_category(session, name="Proteins")

    with pytest.raises(UnknownProductCategoryAssignmentError):
        remove_product_category(
            session, product_id=product.id, category_id=category.id
        )


def test_assignment_requires_existing_product_and_category(session: Session) -> None:
    category = create_category(session, name="Proteins")
    product = make_product(session)

    with pytest.raises(UnknownCategoryProductError):
        assign_product_category(
            session, product_id=uuid.uuid4(), category_id=category.id
        )
    with pytest.raises(UnknownCategoryError):
        assign_product_category(
            session, product_id=product.id, category_id=uuid.uuid4()
        )


def test_deactivation_preserves_assignments_and_reactivation_allows_new_ones(
    session: Session,
) -> None:
    category = create_category(session, name="Beverages")
    first = make_product(session, "Kombucha")
    second = make_product(session, "Sparkling Tea")
    existing = set_primary_category(
        session, product_id=first.id, category_id=category.id
    )

    set_category_active(session, category_id=category.id, is_active=False)

    assert category.is_active is False
    assert existing.is_primary is True
    assert existing.category is category
    with pytest.raises(InactiveCategoryError):
        assign_product_category(
            session,
            product_id=second.id,
            category_id=category.id,
            is_primary=True,
        )
    assert existing.is_primary is True
    assert session.scalar(
        select(func.count()).select_from(ProductCategory).where(
            ProductCategory.product_id == second.id
        )
    ) == 0

    set_category_active(session, category_id=category.id, is_active=True)
    new_assignment = set_primary_category(
        session, product_id=second.id, category_id=category.id
    )
    assert new_assignment.category is category


def test_active_category_listing_is_stable_and_excludes_inactive(
    session: Session,
) -> None:
    zeta = create_category(session, name="Zeta", sort_order=10)
    beta = create_category(session, name="Beta", sort_order=10)
    first = create_category(session, name="First", sort_order=1)
    inactive = create_category(
        session, name="Hidden", sort_order=0, is_active=False
    )

    listed = list_active_categories(session)

    assert listed == [first, beta, zeta]
    assert inactive not in listed


def test_database_enforces_one_primary_per_product(session: Session) -> None:
    product = make_product(session)
    first = create_category(session, name="First")
    second = create_category(session, name="Second")
    session.add_all(
        [
            ProductCategory(
                product=product,
                category=first,
                is_primary=True,
                source=FieldSource.HUMAN,
                verified=True,
                locked=True,
            ),
            ProductCategory(
                product=product,
                category=second,
                is_primary=True,
                source=FieldSource.HUMAN,
                verified=True,
                locked=True,
            ),
        ]
    )

    with pytest.raises(IntegrityError, match="product_categories.product_id"):
        session.flush()


def test_failed_primary_replacement_rolls_back_to_old_primary(
    session: Session, monkeypatch
) -> None:
    product = make_product(session)
    first = create_category(session, name="First")
    second = create_category(session, name="Second")
    old = set_primary_category(
        session, product_id=product.id, category_id=first.id
    )
    new = assign_product_category(
        session, product_id=product.id, category_id=second.id
    )
    session.commit()
    original_flush = session.flush
    flush_calls = 0

    def fail_second_flush(*args, **kwargs):
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == 2:
            raise RuntimeError("promotion failed")
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", fail_second_flush)
    with pytest.raises(RuntimeError, match="promotion failed"):
        set_primary_category(
            session,
            product_id=product.id,
            category_id=second.id,
            replace_primary=True,
        )
    session.rollback()

    assert session.get(ProductCategory, old.id).is_primary is True
    assert session.get(ProductCategory, new.id).is_primary is False


def test_caller_rollback_removes_category_and_assignment(session: Session) -> None:
    product = make_product(session)
    session.commit()
    category = create_category(session, name="Temporary")
    assignment = set_primary_category(
        session, product_id=product.id, category_id=category.id
    )

    session.rollback()

    assert session.get(Category, category.id) is None
    assert session.get(ProductCategory, assignment.id) is None
