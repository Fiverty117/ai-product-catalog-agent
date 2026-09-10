from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.photos import router as photos_router

app = FastAPI(
    title="AI Product Catalog Agent API",
    version="0.1.0",
)

app.include_router(health_router)
app.include_router(photos_router)
