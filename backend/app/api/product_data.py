import uuid
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import ProductCategoryEditRequest, ProductDataSummary, ProductIdentityEditRequest, ProductPriceEditRequest, ProductSKUEditRequest
from app.services.product_data_editorial import (
    ProductDataConflictError, UnknownProductDataError, add_product_sku,
    change_product_price, edit_product_categories, edit_product_identity,
    edit_product_sku, get_product_data,
)

router = APIRouter(prefix="/api/products", tags=["product-data-editorial"])
DB = Annotated[Session, Depends(get_db)]


def _save(session: Session, product_id: uuid.UUID, action: Callable[[], None]) -> ProductDataSummary:
    try:
        action()
        session.commit()
        return get_product_data(session, product_id)
    except UnknownProductDataError as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ProductDataConflictError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Product data conflicts with an existing identity or assignment.") from exc


@router.get("/{product_id}/data", response_model=ProductDataSummary)
def read_product_data(product_id: uuid.UUID, session: DB) -> ProductDataSummary:
    try:
        return get_product_data(session, product_id)
    except UnknownProductDataError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/{product_id}/data/identity", response_model=ProductDataSummary)
def save_identity(product_id: uuid.UUID, request: ProductIdentityEditRequest, session: DB) -> ProductDataSummary:
    return _save(session, product_id, lambda: edit_product_identity(session, product_id, request))


@router.put("/{product_id}/data/categories", response_model=ProductDataSummary)
def save_categories(product_id: uuid.UUID, request: ProductCategoryEditRequest, session: DB) -> ProductDataSummary:
    return _save(session, product_id, lambda: edit_product_categories(session, product_id, request))


@router.put("/{product_id}/data/skus/{sku_id}", response_model=ProductDataSummary)
def save_sku(product_id: uuid.UUID, sku_id: uuid.UUID, request: ProductSKUEditRequest, session: DB) -> ProductDataSummary:
    return _save(session, product_id, lambda: edit_product_sku(session, product_id, sku_id, request))


@router.post("/{product_id}/data/skus", response_model=ProductDataSummary, status_code=201)
def add_sku(product_id: uuid.UUID, request: ProductSKUEditRequest, session: DB) -> ProductDataSummary:
    return _save(session, product_id, lambda: add_product_sku(session, product_id, request))


@router.post("/{product_id}/data/skus/{sku_id}/prices", response_model=ProductDataSummary, status_code=201)
def save_price(product_id: uuid.UUID, sku_id: uuid.UUID, request: ProductPriceEditRequest, session: DB) -> ProductDataSummary:
    return _save(session, product_id, lambda: change_product_price(session, product_id, sku_id, request))
