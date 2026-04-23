"""Model alias resolver — maps client model names to specific provider+model pairs."""
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.db import ModelAlias


async def resolve_alias(db: AsyncSession, model: Optional[str]) -> Optional[ModelAlias]:
    """Return the ModelAlias row for this model name, or None if no alias exists."""
    if not model:
        return None
    result = await db.execute(select(ModelAlias).where(ModelAlias.alias == model))
    return result.scalar_one_or_none()
