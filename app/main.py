from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.database import check_database_connection, engine
from app.api.router import api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    check_database_connection()

    yield

    # Shutdown
    engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="API for MODIS and VIIRS active fire detection analysis.",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return {
        "application": settings.APP_NAME,
        "message": "Welcome to FireScope API!",
        "version": settings.APP_VERSION,
    }


app.include_router(
    api_router,
    prefix=settings.API_V1_PREFIX,
)