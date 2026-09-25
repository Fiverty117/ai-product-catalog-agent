from fastapi import FastAPI

from app.api.catalog_builder import router as catalog_builder_router
from app.api.categories import router as categories_router
from app.api.health import router as health_router
from app.api.photos import router as photos_router
from app.api.product_copy import router as product_copy_router
from app.api.product_data import router as product_data_router
from app.api.product_images import router as product_images_router
from app.api.product_intake import router as product_intake_router

app = FastAPI(
    title="AI Product Catalog Agent API",
    version="0.1.0",
)

app.include_router(health_router)
app.include_router(photos_router)
app.include_router(catalog_builder_router)
app.include_router(categories_router)
app.include_router(product_copy_router)
app.include_router(product_data_router)
app.include_router(product_images_router)
app.include_router(product_intake_router)
