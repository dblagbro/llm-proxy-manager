"""
Provider router — selects the best available provider+model for a request.
Integrates circuit breaker, LMRH hint scoring, and CoT-E auto-engagement.

litellm wire-binding helpers (build_litellm_model, build_litellm_kwargs,
PROVIDER_TYPE_TO_LITELLM, PROVIDER_DEFAULT_MODELS, etc.) were extracted
to ``app.routing.litellm_binding`` 2026-06-02 and are re-exported here
for callers that import them from ``app.routing.router``.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any, Set

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.config import settings
from app.models.db import Provider, ModelCapability, ProviderMetric
from app.routing.circuit_breaker import is_available
from app.routing.lmrh import (
    LMRHHint, CapabilityProfile, rank_candidates_with_scores, build_capability_header
)
from app.routing.capability_inference import infer_capability_profile
# Re-export the litellm-binding helpers from their new home so every
# existing ``from app.routing.router import build_litellm_model, …``
# call site keeps working unchanged.
from app.routing.litellm_binding import (  # noqa: F401  (re-export)
    PROVIDER_TYPE_TO_LITELLM,
    PROVIDER_DEFAULT_MODELS,
    build_litellm_model,
    build_litellm_kwargs,
    resolve_chat_model_for_provider,
    _is_embedding_model,
    _model_family_provider_types,
    _native_thinking_params,
    _O_SERIES,
)

logger = logging.getLogger(__name__)


_TOOL_SUCCESS_HARD_SKIP_THRESHOLD = 0.3  # candidates with rate below this are -inf'd


def _apply_tool_success_weighting(ranked: list[tuple]) -> list[tuple]:
    """v3.8.5 (#265): de-prioritize candidates with low rolling tool-call
    success rate when the request has tools=[].

    Each tuple is (CapabilityProfile, unmet_set, score). When profile
    has ``tool_call_success_rate`` set:
      - rate < HARD_SKIP_THRESHOLD → score = -inf (hard skip)
      - rate >= HARD_SKIP_THRESHOLD → score *= rate (proportional penalty)

    Candidates with ``tool_call_success_rate=None`` are unaffected
    (no probe data yet — defer to the binary native_tools flag).
    """
    out = []
    for profile, unmet, score in ranked:
        rate = getattr(profile, "tool_call_success_rate", None)
        if rate is None:
            out.append((profile, unmet, score))
            continue
        try:
            rate_f = float(rate)
        except (TypeError, ValueError):
            out.append((profile, unmet, score))
            continue
        if rate_f < _TOOL_SUCCESS_HARD_SKIP_THRESHOLD:
            out.append((profile, unmet, float("-inf")))
        else:
            # Score is currently positive (rank_candidates returns floats).
            # Multiplying by rate (0.3-1.0) penalizes weaker tool models
            # proportionally without losing the existing score ordering
            # among same-rate candidates.
            out.append((profile, unmet, score * rate_f))
    return out


def _max_output_for(model_id: str) -> int | None:
    """v5.22.13 — model output cap from the pricing catalog, or None."""
    try:
        from app.monitoring.pricing import max_output_tokens_for
        return max_output_tokens_for(model_id)
    except Exception:
        return None


def _capability_fit(profile, *, has_tools: bool, needs_reasoning: bool,
                    has_images: bool, est_input_tokens: Optional[int],
                    requested_max_tokens: int | None = None) -> Optional[str]:
    """Capability-fit gate (v4.1). Returns None when the provider can serve
    the request — natively or via emulation — or a short reason when it
    CANNOT and should be skipped (operator directive 2026-05-17, "simulate
    if we can, skip if we can't").

    'cannot' cases:
      - vision: a non-vision provider for an image request is SKIPPED, not
        silently stripped.
      - context: a request larger than the provider's context window —
        a hard physical limit, hard skip.

    Note (v4.1.1): tools+reasoning is NO LONGER a skip — the co-emulation
    path (a reasoning-prefixed tool prompt) serves both on a provider native
    in neither, so it is emulable. ``has_tools`` / ``needs_reasoning`` are
    kept for signature stability and future capability checks.
    """
    if has_images and not profile.native_vision:
        return "no native vision"
    if (est_input_tokens and profile.context_length
            and est_input_tokens > profile.context_length):
        return f"context window {profile.context_length} < ~{est_input_tokens} tokens"
    # v5.22.13 — OUTPUT-side twin of the context check above. Without it the
    # router happily picked a provider that physically cannot emit the
    # requested max_tokens; the upstream then rejected it as a client error.
    # On 2026-08-18 callers asked Cohere for max_tokens=32000 against an 8192
    # cap, and the rejection (mis-filed as upstream_5xx) tripped a healthy
    # provider's breaker. None = unknown cap = do not filter.
    if (requested_max_tokens and profile.max_output_tokens
            and requested_max_tokens > profile.max_output_tokens):
        return (f"max output {profile.max_output_tokens} < requested "
                f"{requested_max_tokens} tokens")
    return None


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
    # v4.1 — providers the capability-fit gate skipped (name, reason).
    capability_skipped: list = field(default_factory=list)
    # v3.0.36: cross-family fallback signalling. When the caller asked for
    # a model in family X but no family-X provider was available, we fell
    # back to a different family. ``served_model_native`` is the native
    # (un-prefixed) model id to send upstream — chat handlers rewrite
    # body['model'] to this when the dispatcher (codex-oauth, claude-oauth)
    # reads from body rather than from litellm_model.
    cross_family_fallback: bool = False
    requested_model: Optional[str] = None
    served_model_native: Optional[str] = None
    # v5.0.0 compliance — set when a banned company's provider was filtered
    # AND a cross-family fallback served the request. Surfaces to the
    # disclosure-header builder in _request_pipeline.py (Agent 2 reads these).
    compliance_substituted: bool = False
    compliance_blocked_company: Optional[str] = None
    compliance_served_company: Optional[str] = None


# The litellm wire-binding helpers (_is_embedding_model,
# resolve_chat_model_for_provider, _model_family_provider_types,
# _native_thinking_params, PROVIDER_TYPE_TO_LITELLM,
# PROVIDER_DEFAULT_MODELS, build_litellm_model, build_litellm_kwargs)
# now live in ``app.routing.litellm_binding`` and are re-exported at
# the top of this file. See the module docstring of litellm_binding.py
# for the split rationale.


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
            max_output_tokens=_max_output_for(model_id),
            regions=cap.regions or [],
            modalities=cap.modalities or ["text"],
            native_reasoning=cap.native_reasoning or False,
            native_tools=cap.native_tools if cap.native_tools is not None else True,
            native_vision=cap.native_vision if cap.native_vision is not None else False,
            priority=provider.priority,
            # v3.8.5 (#265): rolling tool-call success rate from the prober.
            # None = no probe data yet → router falls back to binary
            # native_tools. When non-None and the request has tools=[],
            # _apply_tool_success_weighting de-prioritizes low-rate candidates.
            tool_call_success_rate=getattr(cap, "tool_call_success_rate", None),
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
    exclude_provider_ids: Optional[set[str]] = None,
    prefer_cheapest: bool = False,
    sort_mode: Optional[str] = None,
    excluded_provider_types: Optional[set[str]] = None,
    api_key_id: Optional[str] = None,
    dry_run: bool = False,
    est_input_tokens: Optional[int] = None,
    # v5.22.13 — requested output length, for the output-cap gate in
    # _capability_fit. Defaults to None so the 18 existing call sites keep
    # their current behaviour; opt in per caller.
    requested_max_tokens: int | None = None,
    blocked_companies: Optional[Set[str]] = None,
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

    # v3.7.1 — auto-rotation skip. Filter out providers currently
    # marked as at-capacity by ``external_rotation`` rules. The skip
    # is timestamp-bounded; once ``auto_skip_until`` passes we
    # automatically include the provider again. Operator-set
    # ``priority`` and ``enabled`` are preserved unchanged — this
    # filter is purely additive.
    from app.routing.external_rotation import (
        is_currently_at_capacity,
        get_utilization_map,
        reorder_claude_oauth_by_utilization,
        _bucket_size_setting,
        _utilization_bucket,
    )
    pre_filter_count = len(providers)
    providers = [p for p in providers if not is_currently_at_capacity(p)]

    # v4.4 M-4 (Path A) — per-node bridge auth filter. For providers
    # tagged ``node_local_session=True`` in extra_config (today only
    # grok-web when running on the per-node-bridge topology), each
    # node consults its OWN row in provider_node_auth_state. If the
    # local row is not ``auth_state="ok"``, skip this provider on
    # THIS node. The cluster-sync layer ensures peers see the same
    # row eventually; their own routing then independently decides.
    #
    # When the provider's ``node_local_session`` flag is absent or
    # False (the default for all providers today), this filter is a
    # no-op — the routing path is unchanged.
    #
    # Backward-compatible: the table may be empty (M-3's writer not
    # yet wired or running on this node), in which case
    # ``is_local_node_routable(None) == False`` → the provider gets
    # filtered. That's deliberate: a node with no auth-state
    # observation hasn't proven itself capable yet, so don't route
    # to it. M-3 starts filling rows immediately on probe outcome.
    from app.routing.node_auth_state import read_state as _read_local_auth_state
    from app.routing.node_auth_state import is_local_node_routable as _is_node_routable
    _kept: list = []
    _node_filtered: list[tuple[str, str]] = []
    for _p in providers:
        _ec = _p.extra_config or {}
        if not _ec.get("node_local_session"):
            _kept.append(_p)
            continue
        try:
            _state = await _read_local_auth_state(db, _p.id)
        except Exception:
            _state = None
        if _is_node_routable(_state):
            _kept.append(_p)
        else:
            _node_filtered.append((_p.name, (_state.auth_state if _state else "never_authed")))
    if _node_filtered:
        import logging as _log
        _log.getLogger(__name__).debug(
            "router.node_local_session_filter skipped=%s", _node_filtered,
        )
    providers = _kept
    # v3.7.4 — utilization-weighted preference for claude-oauth providers.
    # Among claude-oauth entries, rank by (utilization_bucket,
    # operator_priority). The lowest-util account in each bucket
    # wins. Non-claude-oauth providers keep their operator-priority
    # slot unchanged. This expresses "prefer the account with more
    # headroom" without overriding operator-encoded cost-class /
    # account-preference signals.
    # v3.7.20 — hoist util_map to broader scope so the BUG-020 fix in the
    # P2C selection block below can also consult it. Default to empty
    # dict on failure so the downstream bucket check degrades to "all
    # candidates in same bucket" (i.e. no behavior change vs pre-fix).
    util_map: dict[str, float] = {}
    try:
        util_map = await get_utilization_map(db)
        providers = reorder_claude_oauth_by_utilization(
            providers, util_map, bucket_size_pct=_bucket_size_setting(),
        )
    except Exception as _e:
        # Defensive: routing must never crash on a snapshot-table issue.
        import logging as _log
        _log.getLogger(__name__).warning(
            "external_rotation.utilization_reorder_failed err=%s", _e,
        )
    if not providers:
        # Defensive — if every provider is auto-skipped we'd hard-fail
        # the request. Better to fall through to the original list
        # (let the at-capacity provider attempt and probably get
        # throttled by the upstream) than 503 the caller with no
        # alternative. Log loudly so the operator sees it.
        import logging as _log
        _log.getLogger(__name__).warning(
            "external_rotation.all_providers_at_capacity falling_back_to=%d",
            pre_filter_count,
        )
        result = await db.execute(
            select(Provider).where(Provider.enabled == True).order_by(Provider.priority)
        )
        providers = result.scalars().all()

    # Pin to a specific provider when an alias demands it
    if pinned_provider_id:
        providers = [p for p in providers if p.id == pinned_provider_id]
        if not providers:
            raise RuntimeError(f"Aliased provider '{pinned_provider_id}' is not enabled")

    # v5.0.0 compliance pre-filter (decision 19). Drops providers whose
    # owner_company or model-family lineage is in the key's effective
    # blocklist. ComplianceNoSubstituteError propagates to the dispatch
    # layer (translated to 503 there). Resolves blocklist lazily from
    # api_key_id when caller didn't pass it explicitly; skipped entirely
    # when the result is empty so unflipped deployments pay zero overhead.
    #
    # v5.2.0 / Batch V2 — promoted to the fine-grained ``Policy`` bundle.
    # ``get_effective_policy`` collects per-key + system blocked_companies,
    # allowed_companies, blocked_models, allowed_models in one DB round
    # trip. ``filter_providers_v2`` handles fnmatch glob model matching
    # (e.g. "claude-*", "gpt-4-*-turbo"). The v5.0.0 ``filter_providers``
    # is still exported for sites that already constructed a
    # ``Set[str]`` blocklist directly; this routing site uses the v2
    # path because it has the api_key_id and benefits from the full
    # policy resolution.
    if blocked_companies is not None:
        # Caller pre-resolved a bare blocklist (legacy callers like
        # tests / direct cluster-tools). Honor it via the v1 filter
        # to avoid forcing them through Policy construction.
        if blocked_companies:
            from app.compliance import filter_providers
            providers = filter_providers(providers, blocked_companies, model_override)
    elif api_key_id:
        from app.compliance import get_effective_policy, filter_providers_v2
        policy = await get_effective_policy(db, api_key_id)
        if not policy.is_empty():
            providers = filter_providers_v2(providers, policy, model_override)

    # Hedge path: exclude the primary before CB/availability filtering
    if exclude_provider_id:
        providers = [p for p in providers if p.id != exclude_provider_id]
        if not providers:
            raise RuntimeError("No backup provider available (only one provider)")
    # v5.7.13 — cumulative exclusion for empty-success failover. Single
    # exclude_provider_id cannot escape a ping-pong between same-family
    # candidates (e.g. two Google providers that both empty-stream); the
    # streaming guard now passes the full set of empty-failed providers
    # here so the next select_provider picks something outside the
    # already-failed pool (e.g. cursor-oauth when Gemini providers are
    # all empty-failing).
    if exclude_provider_ids:
        providers = [p for p in providers if p.id not in exclude_provider_ids]
        if not providers:
            raise RuntimeError(
                "No provider available after excluding empty-failed candidates "
                f"({len(exclude_provider_ids)} excluded)"
            )

    # v3.0.45: tenant boundary on personal providers. When a provider has
    # owned_by_key_id set, only that key may route to it. Closes the
    # 2026-05-02 paperless-ai-analyzer leak (17k gpt-4o calls in 48h on
    # the operator's personal ChatGPT account because there was no per-
    # provider tenant scope). Null preserves shared-provider behavior.
    #
    # Resolution order: explicit api_key_id arg → contextvar set by chat
    # entry handlers (covers cascade/critique/hedge/grader paths that
    # would otherwise need individual plumbing).
    from app.routing.tenant import current_api_key_id
    effective_key = api_key_id or current_api_key_id.get()
    providers = [
        p for p in providers
        if not p.owned_by_key_id or p.owned_by_key_id == effective_key
    ]
    if not providers:
        raise RuntimeError(
            "No accessible providers — every available provider is owned by "
            "a different key. Provision your own provider records or ask the "
            "operator to grant access."
        )

    # Filter available (circuit breaker + hold-down)
    available = [p for p in providers if await is_available(p.id)]
    if not available:
        raise RuntimeError("All providers are currently unavailable (circuit breakers open)")

    # v5.21.15 — exclude providers whose OAuth token expired long ago.
    # CamReview 2026-08-05: a cursor-oauth provider with a token dead ~4d
    # was still selectable (its bridge returned a 200-EMPTY instead of a
    # 401, so the breaker never tripped) and got cross-family-picked for
    # claude requests, poisoning them with empty completions. A 15-min
    # grace keeps a recently-expired-but-auto-refreshing provider (e.g.
    # claude-oauth, which refreshes in the dispatch path) selectable; only
    # tokens dead well past any refresh window are dropped.
    import time as _time
    _stale_before = _time.time() - 900
    def _token_dead(p) -> bool:
        exp = getattr(p, "oauth_expires_at", None)
        if not exp:
            return False
        try:
            return float(exp) < _stale_before
        except (TypeError, ValueError):
            return False
    _live = [p for p in available if not _token_dead(p)]
    if _live:
        available = _live
    # If EVERY available provider has a dead token, keep the original list
    # rather than 503 — the dispatch/refresh path may still recover one,
    # and a real error beats silently dropping to zero here.

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
        # v3.4.1: select aliases too so a request for ``grok-3`` matches
        # a capability registered under ``x-ai/grok-3`` with ``["grok-3"]``
        # in its aliases list.
        cap_q = await db.execute(
            select(
                ModelCapability.provider_id,
                ModelCapability.model_id,
                ModelCapability.aliases,
            ).where(ModelCapability.provider_id.in_([p.id for p in available]))
        )
        # provider_id → list of (model_id, aliases) tuples
        cap_by_provider: dict[str, list[tuple[str, list[str]]]] = {}
        for pid, mid, als in cap_q.all():
            cap_by_provider.setdefault(pid, []).append((mid, als or []))
        from app.routing.canonical import matches_capability
        def _supports(p: Provider) -> bool:
            caps = cap_by_provider.get(p.id)
            if not caps:
                return True   # never scanned — give it a try
            return any(matches_capability(model_override, mid, als) for mid, als in caps)
        filtered = [p for p in available if _supports(p)]
        if filtered:
            available = filtered
        else:
            # v3.0.46: capability filter would empty the list — same
            # semantic as family filter empty (v3.0.36): substitute the
            # caller's model with the chosen provider's default rather
            # than dispatching the wrong model name and getting an
            # upstream 400.
            #
            # Concrete trigger (operator 2026-05-02): paperless asked
            # for gpt-4o, ownership scope (v3.0.45) blocked Personal
            # OpenAI, codex-oauth was the only openai-shape candidate
            # left. Codex's caps don't include gpt-4o → empty filter →
            # used to fall through with the wrong model → 400.
            # Now: cross_family_fallback set → body['model'] rewritten
            # to codex's default (gpt-5.5) at dispatch, response carries
            # chosen-because=cross-family-fallback for disclosure.
            if not cross_family_fallback:
                cross_family_fallback = True
                cross_family_requested = model_override

    # Load capability profiles
    profiles = [await _load_profile(db, p) for p in available]
    provider_map = {p.id: p for p in available}

    # v3.0.55: when caller specifies a model (model_override), re-derive
    # cost_tier from THAT model name rather than the provider's default.
    # Bug discovered 2026-05-04: paperless requested claude-haiku-4-5
    # (economy) with cost=economy;require, but Devin-Anthropic-Max-VG's
    # default_model is claude-sonnet-4-6 → profile.cost_tier="standard"
    # → hard-filter excluded claude-oauth → cross-family substitution to
    # Vertex Gemini ($1.59 real billing in one day instead of $0
    # subscription). The provider's catalog supports the requested model;
    # the inference was looking at the wrong slug.
    if model_override:
        m = model_override.lower()
        if any(x in m for x in ["opus", "o1", "o3", "o4", "r1", "deepseek-r", "claude-3-7"]):
            requested_tier = "premium"
        elif any(x in m for x in ["sonnet", "gpt-4o", "gemini-2.0", "gpt-4-turbo", "grok-2"]):
            requested_tier = "standard"
        elif any(x in m for x in ["haiku", "flash", "mini", "gpt-3.5", "grok-beta"]):
            requested_tier = "economy"
        else:
            requested_tier = None
        if requested_tier is not None:
            for prof in profiles:
                # Only downshift to economy if we know the family supports
                # the requested model (i.e. the provider could actually
                # serve a Haiku request). Use family-type alignment as the
                # gate — a Vertex provider should not get its tier
                # rewritten just because the caller asked for "haiku".
                provider = provider_map[prof.provider_id]
                fam_types = _model_family_provider_types(model_override) or set()
                if provider.provider_type in fam_types:
                    prof.cost_tier = requested_tier

    # LMRH ranking (with scores so we can identify the top tier for P2C)
    ranked_scored = rank_candidates_with_scores(profiles, hint)
    if not ranked_scored:
        raise RuntimeError("No providers satisfy the required routing constraints (LLM-Hint hard constraints)")

    # v3.8.5 (#265): tool-call success weighting. When the request has
    # tools=[] AND a candidate has prober data, multiply its score by
    # the rolling success rate. Candidates with rate < 0.3 are hard-
    # skipped (-inf) — operators don't want fallback to lock onto a
    # provider that can't do tools at all.
    #
    # Candidates with tool_call_success_rate=None are unaffected — the
    # router falls back to the binary native_tools flag.
    if has_tools and ranked_scored:
        ranked_scored = _apply_tool_success_weighting(ranked_scored)
        # Re-sort by adjusted score
        ranked_scored.sort(key=lambda t: -t[2])
        # Re-filter -inf candidates (would now sort to the bottom but
        # still pollute "next provider in fallback chain" decisions)
        ranked_scored = [t for t in ranked_scored if t[2] != float("-inf")]
        if not ranked_scored:
            raise RuntimeError(
                "All candidates excluded by tool-call success weighting — "
                "every probe-tested provider has < 30% success on tool calls. "
                "Disable AI_TOOL_PROBER_ENABLED or lower the threshold to unblock."
            )

    # ── Capability-fit gate (v4.1) ───────────────────────────────────────────
    # Skip providers that cannot serve a REQUIRED capability even with
    # emulation — vision, the tools+reasoning collision, or context-window
    # overflow (operator directive 2026-05-17: "simulate if we can, skip if
    # we can't"). Never hard-fails: if the gate would empty the candidate
    # list, keep it unchanged and let the best candidate emulate/degrade.
    cot_globally_enabled = getattr(settings, "cot_enabled", True)
    _task_hint = hint.get("task") if hint else None
    needs_reasoning = bool(
        cot_globally_enabled
        and (key_type == "claude-code"
             or (_task_hint and getattr(_task_hint, "value", None) == "reasoning"))
    )
    capability_skipped: list = []
    _fit_kept = []
    for _t in ranked_scored:
        _reason = _capability_fit(
            _t[0], has_tools=has_tools, needs_reasoning=needs_reasoning,
            has_images=has_images, est_input_tokens=est_input_tokens,
            requested_max_tokens=requested_max_tokens,
        )
        if _reason is None:
            _fit_kept.append(_t)
        else:
            capability_skipped.append(
                (_t[0].provider_name or _t[0].provider_id, _reason))
    # v5.21.16 — HARD-FAIL vision requests when no candidate can actually
    # serve the image (operator directive 2026-08-06, CamReview). Before,
    # an empty capability set was silently ignored (kept the full list) and
    # ``vision_stripped`` dropped the image → the caller got a confident,
    # entirely fabricated text answer with no error. Refuse instead. This
    # only fires for image requests with ZERO vision-capable candidates;
    # when a vision-capable provider exists the gate below routes to it.
    if has_images and not _fit_kept:
        from fastapi import HTTPException
        logger.warning(
            "router.vision_hard_fail — image request but no vision-capable "
            "provider available. skipped=%s", capability_skipped,
        )
        raise HTTPException(
            422,
            "No vision-capable provider is available to process the image(s) "
            "in this request. Refusing to answer blind (the image would be "
            f"dropped). Candidates skipped: {capability_skipped[:6]}",
        )
    if _fit_kept and len(_fit_kept) < len(ranked_scored):
        logger.info("router.capability_gate kept=%d skipped=%s",
                    len(_fit_kept), capability_skipped)
        ranked_scored = _fit_kept
    elif not _fit_kept and capability_skipped:
        # never-hard-fail floor — every candidate fell short; keep them all
        # and let the top one emulate/degrade rather than 503 the caller.
        logger.warning(
            "router.capability_gate all %d candidates fell short — degrading "
            "instead of failing: %s", len(ranked_scored), capability_skipped)

    # v3.3.1: dry-run mode for /lmrh/quotes. Caller wants the ranked
    # candidate list — they're not actually dispatching. Return shaped
    # tuples (provider, profile, unmet, score) so the endpoint can
    # render predicted cost/latency without redoing the filtering or
    # scoring. The list is NOT a RouteResult (no winner picked, no
    # litellm_model built); type signature is widened by the caller's
    # ``cast`` since this is a closed-set internal callsite.
    if dry_run:
        return [
            {
                "provider": provider_map[prof.provider_id],
                "profile": prof,
                "unmet": list(unmet),
                "score": float(score),
            }
            for prof, unmet, score in ranked_scored
        ]  # type: ignore[return-value]

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
            hint=hint,
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
            capability_skipped=capability_skipped,
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
        # v3.7.20 — BUG-020 fix. Before falling into the P2C/EWMA random
        # sample, narrow ``top_tier`` to candidates in the lowest
        # utilization bucket. Without this filter, the v3.7.4 reorder is
        # silently overridden: ``rank_candidates_with_scores`` re-sorts
        # by score, and when buckets are tied at the top-of-band, the
        # "candidate with EWMA samples" explicitly wins over the one
        # without. Self-reinforcing — the busy provider stays busy and
        # the lower-utilization alternate never gets sampled. Operator-
        # observed 2026-05-11: VG (49% util) got 1138/1138 claude-oauth
        # requests while Gmail (4% util) got 0.
        #
        # Logic: if any candidate is in a strictly lower bucket than
        # another, drop the higher-bucket candidates from ``top_tier``.
        # Within the same bucket, fall through to existing P2C/EWMA.
        if util_map and len(top_tier) >= 2:
            bucket_size = _bucket_size_setting()
            def _bucket_of(t):
                u = util_map.get(t[0].provider_id)
                return _utilization_bucket(u, bucket_size)
            bucket_min = min(_bucket_of(t) for t in top_tier)
            low_bucket = [t for t in top_tier if _bucket_of(t) == bucket_min]
            if len(low_bucket) < len(top_tier):
                top_tier = low_bucket
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
            best_profile, unmet, _ = top_tier[0]
    provider = provider_map[best_profile.provider_id]

    # CoT-E auto-engagement: ``needs_reasoning`` (request-level — claude-code
    # key or task=reasoning hint, with cot_enabled) was computed for the
    # capability gate above. CoT-E engages only when the CHOSEN provider also
    # lacks native reasoning.
    cot_engaged = needs_reasoning and not best_profile.native_reasoning

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

    # v4.1.1 — tool emulation engages whenever the request has tools and the
    # provider lacks native tools. It is NO LONGER suppressed by cot_engaged:
    # when both are true the handler runs the co-emulation path (a reasoning-
    # prefixed tool prompt), so tools+reasoning are served together.
    tool_emulation = has_tools and not best_profile.native_tools
    vision_stripped = has_images and not best_profile.native_vision

    cap_header = build_capability_header(
        best_profile, unmet, cot_engaged, tool_emulation,
        model_override=(effective_override or model_override or ""),
        hint=hint,
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

    # v5.0.0 — if the cross-family fallback fired AND a blocklist is in
    # force, mark the route so the disclosure header builder can emit the
    # compliance substitution headers (decision 15).
    compliance_substituted = False
    compliance_blocked = None
    compliance_served = None
    if cross_family_fallback and blocked_companies:
        from app.compliance import model_family_to_company, provider_type_to_company
        requested_family_company = model_family_to_company(model_override)
        if requested_family_company and requested_family_company in blocked_companies:
            compliance_substituted = True
            compliance_blocked = requested_family_company
            compliance_served = provider_type_to_company(provider.provider_type)

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
        capability_skipped=capability_skipped,
        cross_family_fallback=cross_family_fallback,
        requested_model=cross_family_requested if cross_family_fallback else None,
        served_model_native=(provider.default_model if cross_family_fallback else None),
        compliance_substituted=compliance_substituted,
        compliance_blocked_company=compliance_blocked,
        compliance_served_company=compliance_served,
    )
