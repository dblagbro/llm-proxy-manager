"""GET /v1/models — OpenAI-compatible model listing."""
import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import Provider, ModelCapability
from app.auth.keys import resolve_api_key_dep

router = APIRouter(tags=["models"])

# v3.0.47: require a valid llmp-* key on /v1/models. Pre-v3.0.47 the
# endpoint was public — anyone hitting the URL got 196 models across
# all enabled providers, including operator-named provider labels
# ('Devin Personal OpenAI ChatGPT', 'Devin-Anthropic-Max-VG', etc.)
# which leak the billing topology. Operator's bar (2026-05-02): "with
# an API key the caller is trusted; without one we shouldn't be giving
# out the catalog." Apply the same auth dependency the chat endpoints
# use. Auth'd callers still get the full list (no tenant scoping yet —
# tenant-scoping is a separate enhancement, not requested).
_AUTH = resolve_api_key_dep()


# v3.0.23 (Q10): infer model "kind" from name patterns so callers can filter
# their dropdowns to the right surface (chat vs embedding vs image vs audio).
# Accurate-enough heuristics for the major providers; specific edge cases can
# be encoded as exact matches over time.
def _infer_kind(model_id: str) -> str:
    m = (model_id or "").lower()
    # Embeddings — OpenAI / Cohere / Google
    if any(p in m for p in ("text-embedding", "embed-", "/embed", "embedding-")):
        return "embedding"
    # Image generation / edit
    if any(p in m for p in ("dall-e", "stable-diffusion", "imagen", "midjourney", "/imag", "gpt-image")):
        return "image"
    # Audio (TTS / STT)
    if any(p in m for p in ("whisper", "tts-", "/tts", "voice-")):
        return "audio"
    # Vision-only (rare; usually chat models that happen to also do vision)
    # Default to chat — covers GPT/Claude/Gemini/Grok/etc.
    return "chat"


@router.get("/v1/models")
async def list_models(
    db: AsyncSession = Depends(get_db),
    _key=Depends(_AUTH),
):
    """v3.0.95: return only ``Provider.default_model`` per enabled provider.

    Background — coordinator-hub team flagged on 2026-05-07 that the same
    ``GET /v1/models`` returned wildly different counts across nodes:
    www01 → 196 models, www02 + GCP → 5 models. Root cause: the endpoint
    walked ``ModelCapability`` rows in addition to defaults, AND
    ``ModelCapability`` is NOT cluster-synced. www01's table had been
    populated by a one-time discovery action (2026-04-20) with rows that
    included clearly-non-existent model names (``gemma-4-26b-a4b-it``,
    ``gemini-3.1-pro-preview-customtools``) — peers had nothing.

    Operator picking a name from www01's 196 would have it work via www01
    but 4xx via www02/GCP. Listing made-up names to callers is misleading.

    Fix: return only ``Provider.default_model`` per enabled provider.
    Deterministic, consistent across nodes, every name is real (operator-
    configured). Callers can still REQUEST other models — the proxy
    doesn't gate routing on /v1/models membership; this only affects
    advertised discoverability.

    ModelCapability rows remain in use for routing (LMRH ``_load_profile``
    falls back to ``infer_capability_profile()`` when no row exists).
    Cluster-syncing ModelCapability is a separate larger fix queued in
    backlog.
    """
    result = await db.execute(
        select(Provider).where(Provider.enabled == True).order_by(Provider.priority)
    )
    providers = result.scalars().all()

    seen: set[str] = set()
    entries: list[dict] = []
    for p in providers:
        if not p.default_model:
            continue
        if p.default_model in seen:
            continue
        seen.add(p.default_model)
        entries.append({
            "id": p.default_model,
            "object": "model",
            "created": int(time.time()),
            "owned_by": p.name,
            # v3.0.23 (Q10): kind tag for client-side filtering.
            "kind": _infer_kind(p.default_model),
        })

    return {"object": "list", "data": entries}
