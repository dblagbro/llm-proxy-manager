from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import event
from app.config import settings
from app.models.db import Base
import logging

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns to existing tables (SQLite doesn't support IF NOT EXISTS for columns)
        for stmt in [
            "ALTER TABLE providers ADD COLUMN hold_down_sec INTEGER",
            "ALTER TABLE providers ADD COLUMN failure_threshold INTEGER",
            "ALTER TABLE system_settings ADD COLUMN updated_at REAL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN spending_cap_usd REAL",
            "ALTER TABLE api_keys ADD COLUMN rate_limit_rpm INTEGER",
        ]:
            try:
                await conn.exec_driver_sql(stmt)
            except Exception:
                pass  # column already exists
    logger.info("Database initialized")


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
