import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, Product, ProductCategory
from app.domain.enums import FieldSource
from app.domain.identity import identity_key_v1
from app.domain.schemas import CategoryCreate


class CategoryError(ValueError):
    pass


class DuplicateCategoryIdentityError(CategoryError):
    def __init__(self, existing_category: Category):
        self.existing_category = existing_category
        super().__init__(
            "Category already exists; use existing Category "
            f"{existing_category.id}"
        )


class UnknownCategoryError(CategoryError):
    pass


class UnknownCategoryProductError(CategoryError):
    pass


class InactiveCategoryError(CategoryError):
    pass


class PrimaryCategoryConflictError(CategoryError):
    pass


class CategoryAssignmentRoleConflictError(CategoryError):
    pass


class UnknownProductCategoryAssignmentError(CategoryError):
    pass


def create_category(
    session: Session,
    *,
    name: str,
    sort_order: int = 1000,
    is_active: bool = True,
) -> Category:
    """Create one explicit Category without committing the caller transaction."""

    request = CategoryCreate(
        name=name,
        sort_order=sort_order,
        is_active=is_active,
    )
    identity_key = identity_key_v1(request.name)
    existing = session.scalar(
        select(Category).where(Category.identity_key == identity_key)
    )
    if existing is not None:
        raise DuplicateCategoryIdentityError(existing)

    category = Category(
        name=request.name,
        sort_order=request.sort_order,
        is_active=request.is_active,
    )
    session.add(category)
    session.flush()
    return category


def assign_product_category(
    session: Session,
    *,
    product_id: uuid.UUID,
    category_id: uuid.UUID,
    is_primary: bool = False,
    replace_primary: bool = False,
) -> ProductCategory:
    """Create or promote one explicit human Product/Category association."""

    _require_bool("is_primary", is_primary)
    _require_bool("replace_primary", replace_primary)
    product = _require_product(session, product_id)
    category = _require_category(session, category_id)
    existing = session.scalar(
        select(ProductCategory).where(
            ProductCategory.product_id == product.id,
            ProductCategory.category_id == category.id,
        )
    )

    if existing is not None and existing.is_primary == is_primary:
        return existing
    if existing is not None and existing.is_primary and not is_primary:
        raise CategoryAssignmentRoleConflictError(
            "Category is already primary; use primary replacement or remove it explicitly"
        )
    if not category.is_active:
        raise InactiveCategoryError(
            f"inactive Category cannot receive a new assignment: {category.id}"
        )

    current_primary = session.scalar(
        select(ProductCategory).where(
            ProductCategory.product_id == product.id,
            ProductCategory.is_primary.is_(True),
        )
    )
    if is_primary and current_primary is not None:
        if not replace_primary:
            raise PrimaryCategoryConflictError(
                f"Product already has primary Category {current_primary.category_id}; "
                "set replace_primary=true for an explicit replacement"
            )
        current_primary.is_primary = False
        session.flush()

    if existing is None:
        existing = ProductCategory(
            product=product,
            category=category,
            is_primary=is_primary,
            source=FieldSource.HUMAN,
            verified=True,
            locked=True,
        )
        session.add(existing)
    else:
        existing.is_primary = True
    session.flush()
    return existing


def set_primary_category(
    session: Session,
    *,
    product_id: uuid.UUID,
    category_id: uuid.UUID,
    replace_primary: bool = False,
) -> ProductCategory:
    return assign_product_category(
        session,
        product_id=product_id,
        category_id=category_id,
        is_primary=True,
        replace_primary=replace_primary,
    )


def remove_product_category(
    session: Session,
    *,
    product_id: uuid.UUID,
    category_id: uuid.UUID,
) -> None:
    """Remove exactly one association; missing associations fail explicitly."""

    _require_product(session, product_id)
    _require_category(session, category_id)
    assignment = session.scalar(
        select(ProductCategory).where(
            ProductCategory.product_id == product_id,
            ProductCategory.category_id == category_id,
        )
    )
    if assignment is None:
        raise UnknownProductCategoryAssignmentError(
            "Product/Category assignment not found"
        )
    session.delete(assignment)
    session.flush()


def set_category_active(
    session: Session,
    *,
    category_id: uuid.UUID,
    is_active: bool,
) -> Category:
    _require_bool("is_active", is_active)
    category = _require_category(session, category_id)
    category.is_active = is_active
    session.flush()
    return category


def list_active_categories(session: Session) -> list[Category]:
    """Return the stable active taxonomy for UI, AI context or catalog sections."""

    return list(
        session.scalars(
            select(Category)
            .where(Category.is_active.is_(True))
            .order_by(Category.sort_order, Category.identity_key, Category.id)
        ).all()
    )


def _require_product(session: Session, product_id: uuid.UUID) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise UnknownCategoryProductError(f"Product not found: {product_id}")
    return product


def _require_category(session: Session, category_id: uuid.UUID) -> Category:
    category = session.get(Category, category_id)
    if category is None:
        raise UnknownCategoryError(f"Category not found: {category_id}")
    return category


def _require_bool(name: str, value: bool) -> None:
    if type(value) is not bool:
        raise CategoryError(f"{name} must be a boolean")
