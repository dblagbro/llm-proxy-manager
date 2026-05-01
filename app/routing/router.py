"""
Provider router — selects the best available provider+model for a request.
Integrates circuit breaker, LMRH hint scoring, and CoT-E auto-engagement.
"""
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any

import litellm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.config import settings
from app.models.db import Provider, ModelCapability, ProviderMetric
from app.routing.circuit_breaker import is_available, record_success, record_failure, is_billing_error
from app.routing.lmrh import (
    LMRHHint, CapabilityProfile, rank_candidates, rank_candidates_with_scores, build_capability_header
)
from app.routing.capability_inference import infer_capability_profile

logger = logging.getLogger(__name__)


_O_SERIES = re.compile(r"^o[0-9]")


@dataclass
class RouteResult:
    provider: Provider
    profile: CapabilityProfile
    litellm_model: str          # e.g. "anthropic/claude-sonnet-4-5" or "openai/gpt-4o"
    litellm_kwargs: dict
    unmet_hints: list[str]
    cot_engaged: bool
    tool_emulation_engaged: bool
    vision_stripped: bool
    capability_header: str
    native_thinking_params: dict = field(default_factory=dict)
    # v3.0.36: cross-family fallback signalling. When the caller asked for
    # a model in family X but no family-X provider was available, we fell
    # back to a different family. ``served_model_native`` is the native
    # (un-prefixed) model id to send upstream — chat handlers rewrite
    # body['model'] to this when the dispatcher (codex-oauth, claude-oauth)
    # reads from body rather than from litellm_model.
    cross_family_fallback: bool = False
    requested_model: Optional[str] = None
    served_model_native: Optional[str] = None


def _is_embedding_model(model: str) -> bool:
    """v3.0.27: detect embedding-only model names. Used to keep embedding
    models off chat dispatch paths — Cohere's chat API explicitly rejects
    `embed-*` slugs with HTTP 400, OpenAI's chat API does the same with
    `text-embedding-*`. Caller's job to route those to /v1/embeddings."""
    if not model:
        return False
    m = model.lower()
    return (m.startswith("embed-") or m.startswith("text-embedding-")
            or m.startswith("embedding-"))


async def resolve_chat_model_for_provider(
    db: AsyncSession, provider: Provider
) -> tuple[Optional[str], Optional[str]]:
    """v3.0.32: shared helper for "what chat-capable model should we dispatch
    to this provider with?".

    Returns ``(chat_model, skip_reason)``:
      - ``(model, None)`` when a chat-capable model is found. Either the
        provider's ``default_model`` is already a chat slug, or one was
        picked from scanned ``ModelCapability`` rows (preferring
        ``command-*`` for cohere or ``gpt-*`` for OpenAI-shape).
      - ``(None, reason)`` when the provider has only embedding-only
        scanned models. Caller should skip the chat-style call.

    Replaces three duplicate copies that landed across v3.0.27/30/31:
      - ``scanner.test_provider`` (UI Test button)
      - ``keepalive._probe_one`` (synthetic 5-min probe)
      - the entry-time guard in ``completions.py`` + ``messages.py``
        (this one stays — it rejects embedding-named caller requests
        with a 400 before they reach select_provider; doesn't need the
        cap-fallback logic).

    The bug class this prevents: ``provider.default_model`` may be an
    embedding-only slug (e.g. Cohere's ``embed-english-v3.0`` is the
    recommended general-purpose default). Dispatching that to a chat
    surface upstream returns 400 and trips the breaker. v3.0.27, 30, 31
    each fixed one of the call sites in isolation; this helper makes
    the next call site impossible to get wrong.
    """
    default = provider.default_model or ""
    if not _is_embedding_model(default):
        return (default or None, None)

    from app.models.db import ModelCapability
    caps = (await db.execute(
        select(ModelCapability.model_id).where(
            ModelCapability.provider_id == provider.id
        )
    )).scalars().all()
    chat_candidates = [c for c in caps if not _is_embedding_model(c)]
    if not chat_candidates:
        return (None, f"provider {provider.id!r} has no chat-capable scanned models")
    # Prefer `command-*` (Cohere chat) or `gpt-*` over the alphabetical
    # default — those are the most reliable chat surfaces for a probe.
    preferred = [c for c in chat_candidates
                 if c.startswith("command-") or c.startswith("gpt-")]
    return ((preferred or sorted(chat_candidates))[0], None)


def _model_family_provider_types(model: str) -> Optional[set[str]]:
    """v3.0.26: map a requested model name to the set of provider types that
    can physically serve it. Returns None when no family is detected (caller
    falls through to the existing capability/scoring path).

    The mapping is intentionally narrow — only well-known prefixes that
    correspond to known SDK shapes. Unknown prefixes (custom finetunes, new
    families) return None and skip the family filter entirely so we don't
    over-restrict legitimate routes.

    DevinGPT report 2026-05-01: claude-sonnet-4-6 was being routed to
    codex-oauth via a v3.0.22 fall-through. This filter is the hard backstop.
    """
    if not model:
        return None
    m = model.lower()
    # Anthropic family — claude-* + their variants. Vertex's claude-on-bedrock
    # would also live here but we don't currently support that wire format.
    if m.startswith("claude-") or m.startswith("claude/"):
        return {"anthropic", "anthropic-direct", "anthropic-oauth", "claude-oauth"}
    # OpenAI family — gpt-*, o1/o3/o4 reasoning series, text-embedding-*,
    # whisper-*, dall-e-*, codex-*. codex-oauth speaks the same wire format
    # but only for its 6 Plus-tier slugs (handled by the v3.0.22 cap filter
    # which runs after this).
    if (m.startswith("gpt-") or m.startswith("o1-") or m.startswith("o3-")
            or m.startswith("o4-") or m.startswith("text-embedding-")
            or m.startswith("whisper-") or m.startswith("dall-e-")
            or m.startswith("codex-")):
        return {"openai", "codex-oauth"}
    # Google / Gemini family.
    if m.startswith("gemini-") or m.startswith("text-bison") or m.startswith("chat-bison"):
        return {"google", "vertex", "vertex_ai"}
    # Cohere family — their embed-* + command-* slugs.
    if m.startswith("embed-") or m.startswith("command-"):
        return {"cohere"}
    # Unknown family — don't constrain.
    return None


def _native_thinking_params(provider_type: str, model_id: str) -> dict:
    """Return provider-specific reasoning kwargs to inject when native_reasoning=True."""
    m = model_id.lower()
    if provider_type in ("google", "vertex") and "2.5" in m:
        return {"thinking": {"type": "enabled", "budget_tokens": settings.native_thinking_budget_tokens}}
    if provider_type == "openai" and _O_SERIES.match(m):
        return {"reasoning_effort": settings.native_reasoning_effort}
    return {}


PROVIDER_TYPE_TO_LITELLM = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "gemini",
    "vertex": "vertex_ai",
    "ollama": "ollama",
    "grok": "xai",
    "compatible": "openai",     # OpenAI-compatible uses openai provider with custom base_url
    # v2.7.0: claude-oauth never routes through litellm — messages.py
    # dispatches a direct httpx call to platform.claude.com. The "anthropic"
    # prefix here is only used for the `X-Resolved-Model` response header.
    "claude-oauth": "anthropic",
    # v3.0.15: codex-oauth never routes through litellm either —
    # _codex_oauth_dispatch handles it via direct httpx to chatgpt.com.
    # The "openai" prefix is only used for the X-Resolved-Model header.
    "codex-oauth": "openai",
    # v3.0.23 (Q2): Cohere via litellm (cohere/embed-english-v3.0 etc).
    "cohere": "cohere",
}


PROVIDER_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "google":    "gemini-2.0-flash",
    "vertex":    "gemini-2.0-flash-002",
    "openai":    "gpt-4o",
    "grok":      "grok-2",
    "ollama":    "llama3",
    "compatible": "gpt-4o",
    # Claude Pro Max subscription — caller chooses model at request time.
    "claude-oauth": "claude-sonnet-4-6",
    # ChatGPT Plus/Team/Enterprise subscription via Codex CLI.
    # gpt-5.5 is the Plus default; Pro/Team see different slugs.
    "codex-oauth": "gpt-5.5",
    # v3.0.23 (Q2): Cohere — primarily an embeddings provider but also
    # has rerank/chat surfaces. embed-english-v3.0 is the recommended
    # general-purpose default.
    "cohere": "embed-english-v3.0",
}


def build_litellm_model(provider: Provider, model_override: Optional[str] = None) -> str:
    prefix = PROVIDER_TYPE_TO_LITELLM.get(provider.provider_type, "openai")
    default = PROVIDER_DEFAULT_MODELS.get(provider.provider_type, "gpt-4o")
    model = model_override or provider.default_model or default
    return f"{prefix}/{model}"


def build_litellm_kwargs(provider: Provider) -> dict:
    kwargs: dict[str, Any] = {}
    if provider.api_key:
        kwargs["api_key"] = provider.api_key
    if provider.base_url and provider.provider_type in ("ollama", "compatible"):
        kwargs["api_base"] = provider.base_url
    kwargs["timeout"] = provider.timeout_sec
    return kwargs


async def _load_profile(db: AsyncSession, provider: Provider) -> CapabilityProfile:
    """Load capability profile from DB, or infer from model name."""
    model_id = provider.default_model or ""
    result = await db.execute(
        select(ModelCapability).where(
            ModelCapability.provider_id == provider.id,
            ModelCapability.model_id == model_id,
        )
    )
    cap = result.scalar_one_or_none()
    if cap:
        profile = CapabilityProfile(
            provider_id=provider.id,
            provider_type=provider.provider_type,
            provider_name=provider.name or "",
            model_id=model_id,
            tasks=cap.tasks or ["chat"],
            latency=cap.latency or "medium",
            cost_tier=cap.cost_tier or "standard",
            safety=cap.safety or 3,
            context_length=cap.context_length or 128000,
            regions=cap.regions or [],
            modalities=cap.modalities or ["text"],
            native_reasoning=cap.native_reasoning or False,
            native_tools=cap.native_tools if cap.native_tools is not None else True,
            native_vision=cap.native_vision if cap.native_vision is not None else False,
            priority=provider.priority,
        )
    else:
        profile = infer_capability_profile(provider.id, provider.provider_type, model_id, provider.priority)
        profile.provider_name = provider.name or ""

    # Populate avg_ttft_ms from the most recent metric bucket for LMRH scoring
    metric_res = await db.execute(
        select(ProviderMetric)
        .where(ProviderMetric.provider_id == provider.id)
        .order_by(ProviderMetric.bucket_ts.desc())
        .limit(1)
    )
    recent = metric_res.scalar_one_or_none()
    if recent and recent.avg_ttft_ms:
        profile.avg_ttft_ms = recent.avg_ttft_ms

    # Check daily budget cap: sum today's spend across all metric buckets
    if provider.daily_budget_usd is not None:
        today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cost_res = await db.execute(
            select(func.sum(ProviderMetric.total_cost_usd)).where(
                ProviderMetric.provider_id == provider.id,
                ProviderMetric.bucket_ts >= today_midnight,
            )
        )
        today_cost = cost_res.scalar_one_or_none() or 0.0
        if today_cost >= provider.daily_budget_usd:
            profile.over_daily_budget = True
            logger.info(
                "router.budget_demotion",
                extra={"provider": provider.id, "today_cost": today_cost,
                       "budget": provider.daily_budget_usd},
            )

    return profile


async def select_provider(
    db: AsyncSession,
    hint: Optional[LMRHHint],
    has_tools: bool = False,
    has_images: bool = False,
    key_type: str = "standard",
    pinned_provider_id: Optional[str] = None,
    model_override: Optional[str] = None,
    exclude_provider_id: Optional[str] = None,
    prefer_cheapest: bool = False,
    sort_mode: Optional[str] = None,
    excluded_provider_types: Optional[set[str]] = None,
) -> RouteResult:
    """
    Select the best available provider+model for this request.
    Raises RuntimeError if no providers are available.

    exclude_provider_id: skip this provider (used by hedging to pick a backup).
    prefer_cheapest:     pick the cheapest-tier candidate among those satisfying
                         hard constraints (used by cascade routing as the
                         "cheap first" step). cost_tier ordering: economy <
                         standard < premium. Ties broken by priority.
    sort_mode:           v2.8.0 model-slug shortcut override. One of
                         ``"floor"`` (alias for prefer_cheapest=True),
                         ``"nitro"`` (lowest-TTFT provider via PeakEWMA),
                         ``"exacto"`` (default capability-score ranking,
                         tie-break by priority — opposite of P2C random
                         sample). ``None`` keeps default LMRH behavior.
    """
    if sort_mode == "floor":
        prefer_cheapest = True  # collapse onto the existing cheapest path
    result = await db.execute(
        select(Provider).where(Provider.enabled == True).order_by(Provider.priority)
    )
    providers = result.scalars().all()

    if not providers:
        raise RuntimeError("No providers configured")

    # Pin to a specific provider when an alias demands it
    if pinned_provider_id:
        providers = [p for p in providers if p.id == pinned_provider_id]
        if not providers:
            raise RuntimeError(f"Aliased provider '{pinned_provider_id}' is not enabled")

    # Hedge path: exclude the primary before CB/availability filtering
    if exclude_provider_id:
        providers = [p for p in providers if p.id != exclude_provider_id]
        if not providers:
            raise RuntimeError("No backup provider available (only one provider)")

    # Filter available (circuit breaker + hold-down)
    available = [p for p in providers if await is_available(p.id)]
    if not available:
        raise RuntimeError("All providers are currently unavailable (circuit breakers open)")

    # Hard-block providers explicitly excluded from tool requests
    # (exclude_from_tool_requests=True means "never, even with emulation")
    if has_tools:
        available = [p for p in available if not p.exclude_from_tool_requests]
    if has_tools and not available:
        raise RuntimeError("No providers available for tool requests (all excluded)")

    # v2.8.9: filter out provider types the caller can't use. Internal pipeline
    # callers (cascade cheap-route, CoT cross-provider critique, vision-route,
    # hedging backup) pass ``{"claude-oauth"}`` here because they call litellm
    # directly which can't authenticate with OAuth tokens.
    if excluded_provider_types:
        available = [p for p in available if p.provider_type not in excluded_provider_types]
    if not available:
        raise RuntimeError(
            f"No providers available after excluding types {excluded_provider_types}"
        )

    # v3.0.27: when the caller didn't pin a model, drop providers whose
    # default_model is an embedding-only slug. Otherwise build_litellm_model
    # falls back to provider.default_model and we end up dispatching e.g.
    # cohere/embed-english-v3.0 to a chat call → upstream 400. Real-world
    # trip happened on Devin-Cohere (default_model=embed-english-v3.0)
    # 2026-04-30. Embedding callers go through /v1/embeddings, which does
    # its own provider selection via select_embedding_provider.
    if not model_override:
        available = [
            p for p in available
            if not _is_embedding_model(p.default_model or "")
        ]
        if not available:
            raise RuntimeError(
                "No chat-capable providers available — every reachable provider "
                "has an embeddings-only default model. Specify ``model`` in the "
                "request body, or use POST /v1/embeddings for embedding calls."
            )

    # v3.0.26: model-family vs provider-type compatibility filter.
    # Original v3.0.26 raised 503 on empty intersection (DevinGPT silent-
    # substitution bug) — see commit history.
    #
    # v3.0.36: cross-family fallback. When the family filter would empty
    # the list, we now fall back to the broader pool but mark the route
    # so build_litellm_model substitutes the chosen provider's default
    # chat model (NOT the caller's claude-* string — that would 400
    # upstream). Callers that want hard-403-no-substitution opt out via
    # an explicit LMRH constraint:
    #   LLM-Hint: provider-hint=anthropic-*,claude-oauth;require
    # The LMRH scorer already eliminates non-matching candidates with
    # ;require, so 503 still fires when explicit. The default behavior
    # honors the operator's "cross-emulate, don't fail" preference.
    #
    # The cross-family route is signalled by:
    #   - LLM-Capability response header: chosen-because=cross-family-fallback
    #   - LLM-Capability.requested-model + .served-model both echoed
    #   - LLM-Capability.unmet=(model) so callers see substitution
    cross_family_fallback = False
    cross_family_requested = None
    if model_override:
        family_types = _model_family_provider_types(model_override)
        if family_types is not None:
            family_filtered = [p for p in available if p.provider_type in family_types]
            if family_filtered:
                available = family_filtered
            else:
                # Empty family intersection → flag for downstream so the
                # litellm model gets substituted to the chosen provider's
                # default chat slug. ``available`` stays unfiltered.
                cross_family_fallback = True
                cross_family_requested = model_override

    # v3.0.22: model-supports-by-provider filter. Refines the family filter
    # above with scanned capability data. Conservative: providers with NO
    # scanned capabilities still get a chance (we don't know what they
    # support; let them try and fall through via the existing CB on upstream
    # failure). The fall-through here is now safe because the family filter
    # already excluded provider types that physically can't serve the model.
    if model_override:
        cap_q = await db.execute(
            select(ModelCapability.provider_id, ModelCapability.model_id)
            .where(ModelCapability.provider_id.in_([p.id for p in available]))
        )
        cap_by_provider: dict[str, set[str]] = {}
        for pid, mid in cap_q.all():
            cap_by_provider.setdefault(pid, set()).add(mid)
        def _supports(p: Provider) -> bool:
            caps = cap_by_provider.get(p.id)
            if not caps:
                return True   # never scanned — give it a try
            return model_override in caps
        filtered = [p for p in available if _supports(p)]
        if filtered:
            available = filtered
        # If filter would empty the list, fall through with the family-
        # filtered list. Family filter already guarantees type-compatibility.

    # Load capability profiles
    profiles = [await _load_profile(db, p) for p in available]
    provider_map = {p.id: p for p in available}

    # LMRH ranking (with scores so we can identify the top tier for P2C)
    ranked_scored = rank_candidates_with_scores(profiles, hint)
    if not ranked_scored:
        raise RuntimeError("No providers satisfy the required routing constraints (LLM-Hint hard constraints)")

    # Wave 3 #14 — cascade pre-step: prefer cheapest candidate that satisfies
    # hard constraints. economy < standard < premium, tie-break by priority.
    if prefer_cheapest:
        _COST_ORDER = {"economy": 0, "standard": 1, "premium": 2}
        best_profile, unmet, _ = min(
            ranked_scored,
            key=lambda t: (_COST_ORDER.get(t[0].cost_tier, 1), t[0].priority),
        )
        provider = provider_map[best_profile.provider_id]
        litellm_model = build_litellm_model(provider, model_override)
        litellm_kwargs = build_litellm_kwargs(provider)
        cap_header = build_capability_header(
            best_profile, unmet, False, False,
            model_override=model_override or "",
        )
        return RouteResult(
            provider=provider,
            profile=best_profile,
            litellm_model=litellm_model,
            litellm_kwargs=litellm_kwargs,
            unmet_hints=unmet,
            cot_engaged=False,
            tool_emulation_engaged=False,
            vision_stripped=False,
            capability_header=cap_header,
            native_thinking_params={},
        )

    # v2.8.0 — model-slug sort-mode overrides bypass P2C/PeakEWMA selection
    # because they have explicit semantics:
    #   :nitro  → fastest provider (lowest PeakEWMA TTFT). Falls back to
    #             priority when no samples exist yet.
    #   :exacto → highest capability score, ties broken by priority. No
    #             randomized sample (deterministic given a request).
    if sort_mode == "nitro":
        from app.routing.hedging import peak_ewma
        def _nitro_key(t):
            ewma = peak_ewma(t[0].provider_id)
            # Providers with no telemetry sort AFTER providers with samples
            # (we don't know if they're fast). Within each bucket, lower
            # priority number wins.
            return (0 if ewma is not None else 1, ewma if ewma is not None else 0.0, t[0].priority)
        winner = min(ranked_scored, key=_nitro_key)
        best_profile, unmet, _ = winner
    elif sort_mode == "exacto":
        # Top score; ties broken by priority. Deterministic — no random sample.
        top_score = ranked_scored[0][2]
        top_tier = [t for t in ranked_scored if top_score - t[2] < 1.0]
        winner = min(top_tier, key=lambda t: t[0].priority)
        best_profile, unmet, _ = winner
    else:
        # Wave 3 #13 — PeakEWMA + P2C intra-tier selection (default).
        # Identify candidates within 1.0 score of the top (a loose equality band
        # that catches "essentially tied" profiles). If ≥2 qualify, sample two
        # and pick the one with lower PeakEWMA TTFT (falling back to priority
        # when neither has samples yet).
        from app.routing.hedging import peak_ewma
        import random as _random
        top_score = ranked_scored[0][2]
        top_tier = [t for t in ranked_scored if top_score - t[2] < 1.0]
        if len(top_tier) >= 2:
            c1, c2 = _random.sample(top_tier, 2)
            e1 = peak_ewma(c1[0].provider_id)
            e2 = peak_ewma(c2[0].provider_id)
            if e1 is None and e2 is None:
                winner = c1 if c1[0].priority <= c2[0].priority else c2
            elif e1 is None:
                winner = c2
            elif e2 is None:
                winner = c1
            else:
                winner = c1 if e1 <= e2 else c2
            best_profile, unmet, _ = winner
        else:
            best_profile, unmet, _ = ranked_scored[0]
    provider = provider_map[best_profile.provider_id]

    # CoT-E auto-engagement:
    # Triggered when key_type=claude-code OR LLM-Hint task=reasoning + native_reasoning=false
    # Can be disabled globally via the cot_enabled runtime setting.
    cot_engaged = False
    cot_globally_enabled = getattr(settings, "cot_enabled", True)
    if cot_globally_enabled and not best_profile.native_reasoning:
        task_hint = hint.get("task") if hint else None
        if key_type == "claude-code" or (task_hint and task_hint.value == "reasoning"):
            cot_engaged = True

    # v3.0.36: when the family filter empty-fell-back, the chosen provider
    # is from a different family than the caller asked for. Substitute the
    # provider's default chat model rather than passing the wrong-family
    # slug to litellm (which would 400 upstream). The original requested
    # model is reflected in the LLM-Capability header so callers see the
    # substitution.
    effective_override = model_override
    if cross_family_fallback:
        effective_override = None  # build_litellm_model falls back to provider.default_model
        unmet = list(unmet) + ["model"]
    litellm_model = build_litellm_model(provider, effective_override)
    litellm_kwargs = build_litellm_kwargs(provider)

    native_params: dict = {}
    if best_profile.native_reasoning and not cot_engaged:
        native_params = _native_thinking_params(provider.provider_type, best_profile.model_id)

    tool_emulation = has_tools and not best_profile.native_tools and not cot_engaged
    vision_stripped = has_images and not best_profile.native_vision

    cap_header = build_capability_header(
        best_profile, unmet, cot_engaged, tool_emulation,
        model_override=(effective_override or model_override or ""),
    )
    if cross_family_fallback and cross_family_requested:
        # Append the cross-family-fallback markers + requested vs served.
        # The build_capability_header default emits chosen-because=score —
        # override it here. Append rather than rebuild to preserve the
        # other dim values.
        cap_header = (
            cap_header.replace("chosen-because=score", "chosen-because=cross-family-fallback")
            + f', requested-model={cross_family_requested}, served-model={litellm_model}'
        )

    # v3.0.30: was INFO; demoted to DEBUG. This fired on every routing
    # decision — 2728 lines in a 3h sample on www01, ~99% redundant with
    # the structlog "request" line + the activity_log llm_request entry.
    # Operators who need it can flip the app.routing.router level to DEBUG
    # via /api/settings.
    logger.debug(
        "router.selected provider=%s model=%s cot=%s unmet=%s",
        provider.id, litellm_model, cot_engaged, unmet,
        extra={
            "provider": provider.id,
            "model": litellm_model,
            "cot": cot_engaged,
            "unmet": unmet,
        },
    )

    return RouteResult(
        provider=provider,
        profile=best_profile,
        litellm_model=litellm_model,
        litellm_kwargs=litellm_kwargs,
        unmet_hints=unmet,
        cot_engaged=cot_engaged,
        tool_emulation_engaged=tool_emulation,
        vision_stripped=vision_stripped,
        capability_header=cap_header,
        native_thinking_params=native_params,
        cross_family_fallback=cross_family_fallback,
        requested_model=cross_family_requested if cross_family_fallback else None,
        served_model_native=(provider.default_model if cross_family_fallback else None),
    )
