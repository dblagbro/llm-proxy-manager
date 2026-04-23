"""Provider CRUD, test, model scan, and capability management."""
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.database import get_db
from app.models.db import Provider, ModelCapability
from app.auth.admin import require_admin, AdminUser
from app.providers.scanner import scan_provider_models, test_provider
from app.monitoring.status import register_provider
from app.routing.capability_inference import infer_capability_profile

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ProviderCreate(BaseModel):
    name: str
    provider_type: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    priority: int = 10
    enabled: bool = True
    timeout_sec: int = 30
    exclude_from_tool_requests: bool = False
    hold_down_sec: Optional[int] = None       # None = use global setting
    failure_threshold: Optional[int] = None   # None = use global setting
    daily_budget_usd: Optional[float] = None  # None = unlimited
    extra_config: dict = {}


class ProviderUpdate(ProviderCreate):
    pass


class CapabilityUpdate(BaseModel):
    tasks: list[str]
    latency: str
    cost_tier: str
    safety: int
    context_length: int
    regions: list[str]
    modalities: list[str]
    native_reasoning: bool
    native_tools: bool = True
    native_vision: bool = True


@router.get("")
async def list_providers(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(Provider).order_by(Provider.priority))
    providers = result.scalars().all()
    return [_serialize(p) for p in providers]


@router.post("")
async def create_provider(
    body: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    provider = Provider(
        id=secrets.token_hex(8),
        **body.model_dump(),
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    register_provider(provider.id, provider.provider_type, provider.hold_down_sec, provider.failure_threshold)
    return _serialize(provider)


@router.get("/{provider_id}")
async def get_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    return _serialize(p)


@router.put("/{provider_id}")
async def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    for field, value in body.model_dump().items():
        setattr(p, field, value)
    await db.commit()
    await db.refresh(p)
    register_provider(p.id, p.provider_type, p.hold_down_sec, p.failure_threshold)
    return _serialize(p)


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    await db.delete(p)
    await db.commit()
    return {"ok": True}


@router.patch("/{provider_id}/toggle")
async def toggle_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    p.enabled = not p.enabled
    await db.commit()
    return {"enabled": p.enabled}


@router.post("/{provider_id}/test")
async def test_provider_endpoint(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    result = await test_provider(p)
    return result


@router.post("/{provider_id}/scan-models")
async def scan_models(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    try:
        models = await scan_provider_models(db, p)
        if not models:
            return {"scanned": 0, "models": [], "warning": "No models discovered — check API key and provider type"}
        return {"scanned": len(models), "models": models}
    except Exception as e:
        raise HTTPException(500, f"Model scan failed: {e}")


@router.get("/{provider_id}/model-capabilities")
async def list_capabilities(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(
        select(ModelCapability).where(ModelCapability.provider_id == provider_id)
    )
    caps = result.scalars().all()
    return [_serialize_cap(c) for c in caps]


@router.put("/{provider_id}/model-capabilities/{model_id:path}")
async def upsert_capability(
    provider_id: str,
    model_id: str,
    body: CapabilityUpdate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(
        select(ModelCapability).where(
            ModelCapability.provider_id == provider_id,
            ModelCapability.model_id == model_id,
        )
    )
    cap = result.scalar_one_or_none()
    if cap:
        for f, v in body.model_dump().items():
            setattr(cap, f, v)
        cap.source = "manual"
    else:
        cap = ModelCapability(
            provider_id=provider_id,
            model_id=model_id,
            source="manual",
            **body.model_dump(),
        )
        db.add(cap)
    await db.commit()
    await db.refresh(cap)
    return _serialize_cap(cap)


@router.post("/{provider_id}/model-capabilities/infer")
async def infer_capabilities(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Re-run auto-inference on all existing capability records for this provider."""
    p = await _get_or_404(db, provider_id)
    result = await db.execute(
        select(ModelCapability).where(
            ModelCapability.provider_id == provider_id,
            ModelCapability.source == "inferred",
        )
    )
    caps = result.scalars().all()
    updated = 0
    for cap in caps:
        profile = infer_capability_profile(provider_id, p.provider_type, cap.model_id, p.priority)
        cap.tasks = profile.tasks
        cap.latency = profile.latency
        cap.cost_tier = profile.cost_tier
        cap.safety = profile.safety
        cap.context_length = profile.context_length
        cap.regions = profile.regions
        cap.modalities = profile.modalities
        cap.native_reasoning = profile.native_reasoning
        updated += 1
    await db.commit()
    return {"updated": updated}


async def _get_or_404(db: AsyncSession, provider_id: str) -> Provider:
    result = await db.execute(select(Provider).where(Provider.id == provider_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Provider not found")
    return p


def _serialize(p: Provider) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "provider_type": p.provider_type,
        "api_key": f"{p.api_key[:8]}..." if p.api_key else None,
        "base_url": p.base_url,
        "default_model": p.default_model,
        "priority": p.priority,
        "enabled": p.enabled,
        "timeout_sec": p.timeout_sec,
        "exclude_from_tool_requests": p.exclude_from_tool_requests,
        "hold_down_sec": p.hold_down_sec,
        "failure_threshold": p.failure_threshold,
        "daily_budget_usd": p.daily_budget_usd,
        "extra_config": p.extra_config,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _serialize_cap(c: ModelCapability) -> dict:
    return {
        "id": c.id,
        "provider_id": c.provider_id,
        "model_id": c.model_id,
        "tasks": c.tasks,
        "latency": c.latency,
        "cost_tier": c.cost_tier,
        "safety": c.safety,
        "context_length": c.context_length,
        "regions": c.regions,
        "modalities": c.modalities,
        "native_reasoning": c.native_reasoning,
        "native_tools": c.native_tools,
        "native_vision": c.native_vision,
        "source": c.source,
    }
