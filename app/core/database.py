from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import settings
import logging

logger = logging.getLogger("uvicorn")


engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
)


def check_database_connection() -> None:
    """Verify that the PostgreSQL database is reachable."""

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    logger.info("Successfully connected to the PostgreSQL database.")