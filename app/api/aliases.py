"""CRUD for model aliases — admin-only."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import ModelAlias
from app.auth.admin import require_admin, AdminUser
from app.utils.timefmt import utc_iso

router = APIRouter(prefix="/api/aliases", tags=["aliases"])


class AliasBody(BaseModel):
    alias: str
    provider_id: Optional[str] = None
    model_id: str
    description: Optional[str] = None


@router.get("")
async def list_aliases(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(ModelAlias).order_by(ModelAlias.alias))
    return [_ser(a) for a in result.scalars().all()]


@router.post("")
async def create_alias(
    body: AliasBody,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    existing = await db.get(ModelAlias, body.alias)
    if existing:
        raise HTTPException(409, f"Alias '{body.alias}' already exists")
    alias = ModelAlias(**body.model_dump())
    db.add(alias)
    await db.commit()
    await db.refresh(alias)
    return _ser(alias)


@router.put("/{alias_name}")
async def update_alias(
    alias_name: str,
    body: AliasBody,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    a = await db.get(ModelAlias, alias_name)
    if not a:
        raise HTTPException(404, "Alias not found")
    for field, value in body.model_dump().items():
        setattr(a, field, value)
    await db.commit()
    await db.refresh(a)
    return _ser(a)


@router.delete("/{alias_name}")
async def delete_alias(
    alias_name: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    a = await db.get(ModelAlias, alias_name)
    if not a:
        raise HTTPException(404, "Alias not found")
    await db.delete(a)
    await db.commit()
    return {"ok": True}


def _ser(a: ModelAlias) -> dict:
    return {
        "alias": a.alias,
        "provider_id": a.provider_id,
        "model_id": a.model_id,
        "description": a.description,
        "created_at": utc_iso(a.created_at),
    }
