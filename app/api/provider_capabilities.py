"""v3.9.8 (P5 refactor) — provider capability endpoints extracted from
``app/api/providers.py`` to keep that file under 1000 lines.

Endpoints:
- GET    /api/providers/{id}/model-capabilities
- PUT    /api/providers/{id}/model-capabilities/{model_id:path}
- POST   /api/providers/{id}/model-capabilities/infer

Plus the ``_serialize_cap`` helper.

The router uses the same ``/api/providers`` prefix as providers.py;
registered separately in main.py. ``_get_or_404`` is imported from
providers.py for shared 404 handling.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import ModelCapability
from app.auth.admin import require_admin, AdminUser
from app.routing.capability_inference import infer_capability_profile

router = APIRouter(prefix="/api/providers", tags=["providers"])


from typing import Optional


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
    # v3.5.1 — model-identity fields exposed for operator edit via the
    # Hub capability admin form. Optional + defaulted so older Hub UI
    # clients that don't send them still PUT successfully.
    aliases: list[str] = []
    model_family: Optional[str] = None
    model_variant: Optional[str] = None


@router.get("/{provider_id}/node-auth-states")
async def get_node_auth_states(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v4.4 M-5 — return every node's auth state for this provider.

    The admin UI consumes this for the per-node bridge status display
    (Path A: green/amber/red badge per node + per-node [Re-auth]
    button). For providers without ``node_local_session=True`` set
    in extra_config (the no-op case), the response is the empty list
    if no rows have been written.

    Response shape:
        [{
            "node_id": "llm-proxy2-www1",
            "auth_state": "ok" | "expired" | "needs_reauth" |
                           "never_authed" | "bridge_down",
            "last_ok_at": "<ISO-8601>" | null,
            "last_check_at": "<ISO-8601>" | null,
            "reauth_url": "<URL>" | null,
            "last_error": "<string>" | null
        }, ...]
    """
    from app.routing.node_auth_state import read_all_states
    rows = await read_all_states(db, provider_id)
    return [
        {
            "node_id": r.node_id,
            "auth_state": r.auth_state,
            "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
            "last_check_at": r.last_check_at.isoformat() if r.last_check_at else None,
            "reauth_url": r.reauth_url,
            "last_error": r.last_error,
        }
        for r in rows
    ]


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
        # v3.5.1 — surface the model-identity fields to the Hub UI so
        # the capability admin form can show + edit them.
        "aliases": c.aliases or [],
        "model_family": c.model_family,
        "model_variant": c.model_variant,
    }


from app.api.providers import _get_or_404  # noqa: E402
