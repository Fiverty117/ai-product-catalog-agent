import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db.models import Category
from app.db.session import get_db
from app.domain.identity import clean_identity_display_name
from app.services.categories import (
    CategoryInUseError, DuplicateCategoryIdentityError, UnknownCategoryError,
    category_products, category_usage, create_category, deactivate_unused_category,
    list_managed_categories, rename_category, set_category_active, set_category_order,
)

router = APIRouter(prefix="/api/categories", tags=["categories"])
DB = Annotated[Session, Depends(get_db)]


class CategoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    sort_order: int = Field(default=1000, strict=True, ge=0, le=2147483647)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        cleaned = clean_identity_display_name(value)
        if len(cleaned) > 255:
            raise ValueError("Category name must be at most 255 characters")
        return cleaned


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    sort_order: int | None = Field(default=None, strict=True, ge=0, le=2147483647)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        return CategoryInput.valid_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and self.sort_order is None:
            raise ValueError("Provide a name or order change")
        return self


class CategorySummary(BaseModel):
    id: uuid.UUID
    name: str
    identity_key: str
    is_active: bool
    sort_order: int
    primary_product_count: int
    secondary_product_count: int
    total_product_count: int


class CategoryList(BaseModel):
    items: list[CategorySummary]
    total: int
    all_total: int
    limit: int
    offset: int


class CategoryProduct(BaseModel):
    product_id: uuid.UUID
    product_name: str
    brand_name: str
    role: Literal["primary", "secondary"]


class CategoryDetail(CategorySummary):
    affected_products: list[CategoryProduct]
    affected_products_limit: int = 50


def _summary(session: Session, category: Category) -> CategorySummary:
    return CategorySummary(
        id=category.id, name=category.name, identity_key=category.identity_key,
        is_active=category.is_active, sort_order=category.sort_order,
        **category_usage(session, category.id),
    )


def _begin_write(session: Session) -> None:
    # Serialize the usage check with assignment writes on SQLite.
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _mutate(session: Session, action) -> CategorySummary:
    try:
        _begin_write(session)
        category = action()
        result = _summary(session, category)
        session.commit()
        return result
    except UnknownCategoryError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except DuplicateCategoryIdentityError as exc:
        session.rollback()
        existing = exc.existing_category
        raise HTTPException(409, {"code": "duplicate_category", "message":
            "Category already exists. Reactivate the existing Category." if not existing.is_active else
            "Category already exists.", "existing_category_id": str(existing.id),
            "existing_category_active": existing.is_active}) from exc
    except CategoryInUseError as exc:
        session.rollback()
        raise HTTPException(409, {"code": "category_in_use", "message": str(exc), **exc.usage}) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Category conflicts with an existing Category or concurrent change.") from exc
    except OperationalError as exc:
        session.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise HTTPException(409, "Category is being changed concurrently. Please retry.") from exc
        raise HTTPException(500, "Category operation failed.") from exc


@router.get("", response_model=CategoryList)
def list_categories(
    session: DB, search: str = "", status: Literal["active", "inactive", "all"] = "active",
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CategoryList:
    rows, total = list_managed_categories(session, search=search, status=status, limit=limit, offset=offset)
    return CategoryList(items=[CategorySummary(
        id=category.id, name=category.name, identity_key=category.identity_key,
        is_active=category.is_active, sort_order=category.sort_order, **usage,
    ) for category, usage in rows], total=total,
        all_total=session.scalar(select(func.count()).select_from(Category)) or 0,
        limit=limit, offset=offset)


@router.post("", response_model=CategorySummary, status_code=201)
def create(request: CategoryInput, session: DB) -> CategorySummary:
    return _mutate(session, lambda: create_category(session, name=request.name, sort_order=request.sort_order))


@router.get("/{category_id}", response_model=CategoryDetail)
def detail(category_id: uuid.UUID, session: DB) -> CategoryDetail:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "Category not found")
    return CategoryDetail(**_summary(session, category).model_dump(), affected_products=category_products(session, category_id))


@router.patch("/{category_id}", response_model=CategorySummary)
def update(category_id: uuid.UUID, request: CategoryUpdate, session: DB) -> CategorySummary:
    def action() -> Category:
        category = session.get(Category, category_id)
        if category is None:
            raise UnknownCategoryError(f"Category not found: {category_id}")
        if request.name is not None:
            category = rename_category(session, category_id=category_id, name=request.name)
        if request.sort_order is not None:
            category = set_category_order(session, category_id=category_id, sort_order=request.sort_order)
        return category
    return _mutate(session, action)


@router.post("/{category_id}/deactivate", response_model=CategorySummary)
def deactivate(category_id: uuid.UUID, session: DB) -> CategorySummary:
    return _mutate(session, lambda: deactivate_unused_category(session, category_id=category_id))


@router.post("/{category_id}/reactivate", response_model=CategorySummary)
def reactivate(category_id: uuid.UUID, session: DB) -> CategorySummary:
    return _mutate(session, lambda: set_category_active(session, category_id=category_id, is_active=True))
