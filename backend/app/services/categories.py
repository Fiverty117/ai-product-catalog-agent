import uuid

from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from app.db.models import Brand, Category, Product, ProductCategory
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


class CategoryInUseError(CategoryError):
    def __init__(self, usage: dict[str, int]):
        self.usage = usage
        super().__init__("Reassign or remove Product classifications before deactivating this Category.")


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


def category_usage(session: Session, category_id: uuid.UUID) -> dict[str, int]:
    primary, secondary, total = session.execute(
        select(
            func.count(distinct(case((ProductCategory.is_primary.is_(True), ProductCategory.product_id)))),
            func.count(distinct(case((ProductCategory.is_primary.is_(False), ProductCategory.product_id)))),
            func.count(distinct(ProductCategory.product_id)),
        ).where(ProductCategory.category_id == category_id)
    ).one()
    return {
        "primary_product_count": primary,
        "secondary_product_count": secondary,
        "total_product_count": total,
    }


def list_managed_categories(
    session: Session, *, search: str = "", status: str = "active", limit: int = 100, offset: int = 0
) -> tuple[list[tuple[Category, dict[str, int]]], int]:
    query = select(Category)
    if status != "all":
        query = query.where(Category.is_active.is_(status == "active"))
    if search.strip():
        query = query.where(Category.identity_key.contains(identity_key_v1(search), autoescape=True))
    count = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    categories = session.scalars(
        query.order_by(Category.sort_order, Category.identity_key, Category.id).limit(limit).offset(offset)
    ).all()
    return [(category, category_usage(session, category.id)) for category in categories], count


def category_products(session: Session, category_id: uuid.UUID, *, limit: int = 50) -> list[dict[str, str]]:
    rows = session.execute(
        select(Product.id, Product.name, Brand.name, ProductCategory.is_primary)
        .join(ProductCategory, ProductCategory.product_id == Product.id)
        .join(Brand, Brand.id == Product.brand_id)
        .where(ProductCategory.category_id == category_id)
        .order_by(Brand.identity_key, Product.identity_key, Product.id)
        .limit(limit)
    ).all()
    return [
        {"product_id": str(product_id), "product_name": product_name, "brand_name": brand_name,
         "role": "primary" if is_primary else "secondary"}
        for product_id, product_name, brand_name, is_primary in rows
    ]


def rename_category(session: Session, *, category_id: uuid.UUID, name: str) -> Category:
    category = _require_category(session, category_id)
    cleaned = CategoryCreate(name=name).name
    key = identity_key_v1(cleaned)
    existing = session.scalar(select(Category).where(Category.identity_key == key, Category.id != category_id))
    if existing is not None:
        raise DuplicateCategoryIdentityError(existing)
    category.name = cleaned
    session.flush()
    return category


def set_category_order(session: Session, *, category_id: uuid.UUID, sort_order: int) -> Category:
    category = _require_category(session, category_id)
    category.sort_order = CategoryCreate(name=category.name, sort_order=sort_order).sort_order
    session.flush()
    return category


def deactivate_unused_category(session: Session, *, category_id: uuid.UUID) -> Category:
    category = _require_category(session, category_id)
    usage = category_usage(session, category_id)
    if usage["total_product_count"]:
        raise CategoryInUseError(usage)
    category.is_active = False
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
