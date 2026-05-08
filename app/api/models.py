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
    defaults_only: bool = False,
):
    """v3.1.7: return the full ``ModelCapability`` catalog (one entry per
    provider × model_id) by default, plus each provider's ``default_model``
    for completeness.

    History:

    - Pre-v3.0.95: returned full catalog. Cross-node inconsistency: www01
      had 196 models, www02 + GCP had 5. Root cause: ``ModelCapability``
      wasn't cluster-synced; one-time discoveries on www01 leaked into the
      response and listed non-existent model names (``gemma-4-26b-a4b-it``,
      etc.). Hub callers picking from those names got 4xx on peers.
    - v3.0.95: switched to defaults-only (one entry per enabled provider).
      Deterministic across nodes; only real operator-configured names.
      Trade-off: callers couldn't see the full catalog for autocomplete /
      model-picker UIs.
    - v3.1.2: bulk catalog cluster-sync shipped — ModelCapability now
      replicates across all 4 nodes deterministically (LWW by updated_at).
      The v3.0.95 cross-node-inconsistency rationale no longer applies.
    - v3.1.7 (this version): default back to full catalog. Coordinator-hub
      team's "Scan Models" UI expects to see all available models; 5 was
      surprising-low for a fleet with ~670 catalog entries (304 indexed
      from claude-oauth + 367 from OpenRouter + various Google/OpenAI/
      Cohere). Pass ``?defaults_only=true`` to restore the v3.0.95
      single-entry-per-provider view (useful for cluster-status checks
      where the full catalog is overkill).

    Identity is ``(provider_id, model_id)``; we de-dupe on ``model_id``
    alone in the response (callers identify models by name, not by which
    provider serves them — and routing handles provider selection).
    Defaults are merged in last so a provider's default_model surfaces
    even if no ModelCapability row exists for it.
    """
    result = await db.execute(
        select(Provider).where(Provider.enabled == True).where(Provider.deleted_at.is_(None)).order_by(Provider.priority)
    )
    providers = result.scalars().all()
    provider_by_id = {p.id: p for p in providers}
    enabled_provider_ids = set(provider_by_id.keys())
    now = int(time.time())

    seen: set[str] = set()
    entries: list[dict] = []

    if not defaults_only:
        # Walk ModelCapability rows for enabled providers. Sort by
        # provider priority then model_id for stable output ordering.
        cap_result = await db.execute(
            select(ModelCapability).where(
                ModelCapability.provider_id.in_(enabled_provider_ids),
                ModelCapability.deleted_at.is_(None),
            )
        )
        caps = cap_result.scalars().all()
        # Stable sort: priority asc, model_id asc
        caps_sorted = sorted(caps, key=lambda c: (
            provider_by_id[c.provider_id].priority,
            c.model_id,
        ))
        for c in caps_sorted:
            if c.model_id in seen:
                continue
            seen.add(c.model_id)
            p = provider_by_id[c.provider_id]
            entries.append({
                "id": c.model_id,
                "object": "model",
                "created": now,
                "owned_by": p.name,
                "kind": _infer_kind(c.model_id),
            })

    # Always include each enabled provider's default_model — it's the
    # canonical "what this provider serves by default" entry, useful when
    # admin hasn't run scan-models yet.
    for p in providers:
        if not p.default_model:
            continue
        if p.default_model in seen:
            continue
        seen.add(p.default_model)
        entries.append({
            "id": p.default_model,
            "object": "model",
            "created": now,
            "owned_by": p.name,
            "kind": _infer_kind(p.default_model),
        })

    return {"object": "list", "data": entries}
