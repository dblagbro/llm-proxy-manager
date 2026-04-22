"""
GET  /api/settings  — return current effective values (DB overrides + env defaults)
PUT  /api/settings  — persist a partial update and apply live
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.admin import require_admin, AdminUser
from app import config_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_settings(
    _user: AdminUser = Depends(require_admin),
):
    defaults = config_runtime.get_defaults()
    # Overlay with whatever is currently live on the settings singleton
    from app.config import settings as s
    result = {}
    for key, meta in config_runtime.SCHEMA.items():
        if hasattr(s, key):
            result[key] = getattr(s, key)
        else:
            result[key] = defaults[key]
    return result


@router.put("")
async def put_settings(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _user: AdminUser = Depends(require_admin),
):
    unknown = [k for k in body if k not in config_runtime.SCHEMA]
    if unknown:
        raise HTTPException(400, f"Unknown setting keys: {unknown}")
    await config_runtime.save(db, body)
    logger.info("settings_updated", keys=list(body.keys()))
    return {"saved": list(body.keys())}
