"""API key authentication."""
import hashlib
import secrets
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.sql import func

from app.models.db import ApiKey

logger = logging.getLogger(__name__)


@dataclass
class ApiKeyRecord:
    id: str
    name: str
    key_type: str  # standard|claude-code


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Returns (raw_key, key_hash). Raw key is shown once and never stored."""
    raw = "llmp-" + secrets.token_urlsafe(32)
    return raw, _hash_key(raw)


async def verify_api_key(db: AsyncSession, raw_key: Optional[str]) -> ApiKeyRecord:
    if not raw_key:
        raise HTTPException(401, "Missing API key")
    key_hash = _hash_key(raw_key)
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.enabled == True))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(401, "Invalid or disabled API key")

    # Update usage stats (fire-and-forget, non-blocking)
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key.id)
        .values(total_requests=ApiKey.total_requests + 1, last_used_at=func.now())
    )
    await db.commit()

    return ApiKeyRecord(id=key.id, name=key.name, key_type=key.key_type)
