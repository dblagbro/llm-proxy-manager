"""
Shared request-pipeline helpers for /v1/messages and /v1/chat/completions.

Both endpoints run roughly the same preflight recipe before diverging into
wire-format-specific handling:

    verify_api_key →                 (endpoint — auth format differs)
    apply_privacy_filters →          (shared: guard + PII mask)
    build_hint_with_auto_task →      (shared: parse + classify)
    resolve_alias + select_provider → (endpoint — identical)
    apply_context_compression →      (shared: truncate/mapreduce)
    build_base_response_headers →    (shared)

Extracting these four helpers removes ~120 lines of copy-paste between the
two handlers and gives each shared behavior a single place to test.

Each helper is intentionally small and has no hidden state. The wire
format differences (Anthropic message shape vs OpenAI message shape) are
passed in explicitly.
"""
from __future__ import annotations

import logging
from typing import Optional, Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)


# ── 1. Privacy filters: guard + PII mask ─────────────────────────────────────


def apply_privacy_filters(messages_list: list[dict], body: dict) -> tuple[list[dict], int]:
    """Apply Wave 6 semantic prompt guard + PII mask, in that order.

    Guard runs first so the denylist match sees untokenized content.
    PII mask rewrites messages_list and sets body["messages"] to the
    redacted copy so downstream reads pick it up.

    Returns (messages_list, pii_masked_count).
    Raises HTTPException(400) when guard blocks the request.
    """
    # Prompt guard first
    from app.privacy.prompt_guard import check_messages as _guard_check, is_enabled as _guard_enabled
    if _guard_enabled():
        match = _guard_check(messages_list)
        if match:
            raise HTTPException(400, f"Request blocked by prompt guard (pattern: {match!r})")

    # PII masking
    from app.privacy.pii import mask_messages as _pii_mask, is_enabled as _pii_enabled
    pii_count = 0
    if _pii_enabled():
        messages_list, pii_count = _pii_mask(messages_list)
        body["messages"] = messages_list
    return messages_list, pii_count


# ── 2. Hint parsing + auto-classification ────────────────────────────────────


def _extract_last_user_text(messages_list: list[dict]) -> str:
    """Works for both Anthropic (list-of-blocks) and OpenAI (list-of-parts)
    message shapes — the 'text' block/part format is identical between them."""
    for m in reversed(messages_list):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
    return ""


async def build_hint_with_auto_task(llm_hint: Optional[str], messages_list: list[dict]):
    """Parse the LLM-Hint header and, when enabled + no explicit task, auto-classify
    the last user message into a task dimension.

    Returns (hint, auto_task_name_or_None).
    """
    from app.routing.lmrh import parse_hint, LMRHHint, HintDimension

    hint = parse_hint(llm_hint)
    auto_task: Optional[str] = None

    if not settings.task_auto_detect_enabled:
        return hint, auto_task
    if hint is not None and hint.get("task"):
        return hint, auto_task

    user_text = _extract_last_user_text(messages_list)
    if not user_text:
        return hint, auto_task

    from app.routing.classifier import classify
    cls = await classify(
        user_text[:800],
        settings.semantic_cache_embedding_model,
        settings.semantic_cache_embedding_dims,
    )
    if not cls:
        return hint, auto_task

    auto_task, _conf = cls
    if hint is None:
        hint = LMRHHint(raw=f"task={auto_task}")
    hint.dimensions.append(HintDimension("task", auto_task))
    return hint, auto_task


# ── 3. Long-context compression (truncate | mapreduce | error) ───────────────


async def apply_context_compression(
    messages_list: list[dict],
    *,
    route,
    x_context_strategy: Optional[str],
    extra: dict,
    system: str = "",
) -> tuple[list[dict], Optional[str]]:
    """Wave 5 #26 — truncate, map-reduce, or reject when messages exceed the
    selected provider's context window.

    Returns (messages_list, strategy_applied_header_value_or_None).
    Raises HTTPException(413) on strategy=error.
    """
    from app.api.long_context import (
        needs_compression, resolve_strategy, truncate_to_window, mapreduce_compress,
    )

    if not needs_compression(messages_list, route.profile.context_length, system):
        return messages_list, None

    strategy = resolve_strategy(x_context_strategy)
    if strategy == "error":
        tokens_before = len(str(messages_list)) // 3
        raise HTTPException(
            413,
            f"Context window exceeded: ~{tokens_before} tokens > "
            f"{route.profile.context_length} allowed",
        )

    if strategy == "mapreduce":
        user_q = _extract_last_user_text(messages_list)
        messages_list, chunks, _ = await mapreduce_compress(
            messages_list,
            model=route.litellm_model,
            extra=extra,
            context_length=route.profile.context_length,
            user_question=user_q,
        )
        return messages_list, f"mapreduce:{chunks}chunks"

    # truncate (default)
    messages_list, dropped = truncate_to_window(
        messages_list, route.profile.context_length, system,
    )
    return messages_list, f"truncate:{dropped}dropped"


# ── 4. Base response headers ─────────────────────────────────────────────────


def build_base_response_headers(
    *,
    route,
    auto_task: Optional[str],
    vision_routed_count: int,
    context_strategy_applied: Optional[str],
    pii_masked_count: int,
    hint: Optional[Any],
    max_tokens: Optional[int] = None,
) -> dict[str, str]:
    """Build the common set of response headers shared by both endpoints.

    Callers may layer endpoint-specific headers on top (e.g. budget /
    cache / hedge) but everything in this dict is identical between the
    Anthropic and OpenAI handlers.
    """
    headers: dict[str, str] = {
        "X-Provider": route.provider.name,
        "X-Resolved-Provider": route.provider.provider_type,  # Wave 5 #28
        "LLM-Capability": route.capability_header,
        "X-Resolved-Model": route.litellm_model,
    }
    if max_tokens is not None:
        headers["X-Token-Budget-Remaining"] = str(max_tokens)

    # Wave 5 #28 — emulation level
    emul = "minimal"
    if route.tool_emulation_engaged or route.vision_stripped:
        emul = "standard"
    if route.cot_engaged:
        emul = "enhanced"
    headers["X-Emulation-Level"] = emul

    if auto_task:
        headers["X-Task-Auto-Detected"] = auto_task
    if vision_routed_count:
        headers["X-Vision-Routed"] = str(vision_routed_count)
    if context_strategy_applied:
        headers["X-Context-Strategy-Applied"] = context_strategy_applied
    if pii_masked_count:
        headers["X-PII-Masked"] = str(pii_masked_count)

    if hint is not None:
        from app.routing.lmrh import build_hint_set_header
        hint_set = build_hint_set_header(hint, route.unmet_hints)
        if hint_set:
            headers["LLM-Hint-Set"] = hint_set

    return headers


# ── 5. Provider selection (with 503 conversion + auto-model resolution) ──────


async def select_provider_with_503(
    db: AsyncSession,
    hint,
    *,
    has_tools: bool,
    has_images: bool,
    key_record,
    parsed_slug,
    alias,
    detailed_503: bool = True,
):
    """Centralized ``select_provider`` call with RuntimeError → HTTPException(503)
    conversion and the v3.0.22 / v3.0.99 ``model_override`` plumbing both
    endpoints depend on.

    Pre-v3.0.99 only ``/v1/chat/completions`` passed ``parsed_slug.bare_model``
    as ``model_override`` — ``/v1/messages`` passed ``None`` when no
    ``ModelAlias`` row existed, which silently disabled the family +
    capability filters and force-routed gemini probes to claude-oauth
    providers (→ 404 from platform.claude.com). Centralizing here makes
    the parity structural instead of incidental.

    Args:
        detailed_503: When True (used by ``/v1/messages``) include the
            actionable circuit-breaker / no-providers messages. When False
            (used by ``/v1/chat/completions``) emit the generic 503.

    Returns the ``RouteResult`` from ``select_provider``.
    """
    from app.routing.router import select_provider

    # v3.0.22 / v3.0.99 — always pass the requested model name, even when
    # no ModelAlias row resolved it. Activates router.py:431 family filter
    # + the v3.0.22 model-supports-by-provider capability filter +
    # v3.0.46 cross-family-fallback path.
    requested_model = (alias.model_id if alias else parsed_slug.bare_model) or None
    try:
        return await select_provider(
            db,
            hint,
            has_tools=has_tools,
            has_images=has_images,
            key_type=key_record.key_type,
            pinned_provider_id=alias.provider_id if alias else None,
            model_override=requested_model,
            sort_mode=parsed_slug.sort_mode,
            api_key_id=key_record.id,  # v3.0.45 tenant scoping
        )
    except RuntimeError as e:
        msg = str(e)
        if detailed_503 and "circuit breakers open" in msg:
            raise HTTPException(
                503,
                "All providers are currently unavailable (circuit breakers open). "
                "Most common cause: Anthropic server-side OAuth token revocation "
                "trips the 24h auth-failure breaker on every claude-oauth provider. "
                "Operator action: re-auth the affected provider(s) via the OAuth UI, "
                "or wait for the hold-down to expire.",
            )
        if detailed_503 and "No providers configured" in msg:
            raise HTTPException(
                503,
                "No providers configured. Operator action: enable at least one "
                "provider via the Providers page or POST /api/providers.",
            )
        raise HTTPException(503, f"Provider selection failed: {msg}")


def resolve_auto_model_into_body(body: dict, route, is_auto: bool) -> dict:
    """When the caller used ``model: "auto"`` (or ``"llmp-auto"``), substitute
    the resolved model from the chosen route back into ``body["model"]`` so
    downstream dispatch (claude-oauth direct, codex-oauth direct, litellm)
    sees a real model name. Idempotent if ``is_auto`` is False.

    Raises HTTP 502 if auto-routing chose a provider with no
    ``default_model`` to fall back to.
    """
    if not is_auto:
        return body
    resolved_model = route.profile.model_id or route.provider.default_model
    if not resolved_model:
        raise HTTPException(
            502,
            f"auto-routing chose {route.provider.name!r} but it has no default_model set",
        )
    return {**body, "model": resolved_model}


# ── 7. Semantic-cache decision + serve (R1, 2026-05-09) ────────────────────


async def maybe_serve_from_cache(
    *,
    # Decision inputs (cache.middleware.decide_cacheable signature)
    x_cache_header: Optional[str],
    api_key_opt_in: bool,
    key_type: str,
    route,
    has_tools: bool,
    webhook_url: Optional[str],
    body: dict,
    messages_list: list[dict],
    system: Any,                  # Anthropic-only; pass None for OpenAI
    tools: Any,
    x_cache_ttl_header: Optional[str],
    tenant_id: str,
    # Response shape — caller passes the wire-format builders
    endpoint: str,                # "messages" | "completions"
    text_sse_fn,                  # callable(text) -> async generator
    text_response_fn,             # callable(text, model) -> dict
    # Side outputs
    resp_headers: dict,
    stream: bool,
):
    """Run the semantic-cache decision + check + (on hit) build a response.

    Pre-R1 (2026-05-09) this 35-line block was duplicated verbatim between
    ``messages.py`` and ``completions.py``. The orchestration is identical;
    only the SSE / JSON response builders differ. Caller passes the
    builders as ``text_sse_fn`` / ``text_response_fn``.

    Returns ``(cache_decision, response_or_none)`` tuple:

      - ``cache_decision``: the CacheDecision the caller needs to retain
        for the post-response ``maybe_store()`` write-back call. Carries
        the eligibility flag, prompt-hash key, and TTL. Returned in all
        three branches so the caller's later ``try: maybe_store(...)``
        always has a real value (pre-R1 the local was always set; the
        helper's first cut returned None on miss/bypass and the silent
        ``try/except Exception`` swallowed the resulting NameError —
        cache write-back was quietly skipped on every request).
      - ``response_or_none``: when cache hits, a ready-to-return
        ``StreamingResponse`` or ``JSONResponse``; when miss / bypass,
        ``None`` so the caller proceeds with the request.

    Mutates ``resp_headers``: sets ``X-Cache-Status`` to one of
    ``bypass`` / ``miss`` / ``hit``, plus ``X-Cache-Similarity`` on
    hit. Does not mutate any other input.
    """
    from fastapi.responses import StreamingResponse, JSONResponse
    from app.cache.middleware import decide_cacheable, maybe_check

    cache_decision = decide_cacheable(
        x_cache_header=x_cache_header,
        api_key_opt_in=api_key_opt_in,
        key_type=key_type,
        cot_engaged=route.cot_engaged,
        tool_emulation=route.tool_emulation_engaged,
        has_tools=has_tools,
        webhook_url=webhook_url,
        temperature=body.get("temperature"),
        messages=messages_list,
        model=route.litellm_model,
        tenant_id=tenant_id,
        system=system,
        tools=tools,
        x_cache_ttl_header=x_cache_ttl_header,
    )
    resp_headers["X-Cache-Status"] = "bypass" if not cache_decision.eligible else "miss"
    if not cache_decision.eligible:
        return cache_decision, None
    cache_hit = await maybe_check(cache_decision, endpoint=endpoint)
    if not cache_hit:
        return cache_decision, None
    resp_headers["X-Cache-Status"] = "hit"
    resp_headers["X-Cache-Similarity"] = f"{cache_hit.similarity:.3f}"
    if stream:
        return cache_decision, StreamingResponse(
            text_sse_fn(cache_hit.response_text),
            media_type="text/event-stream",
            headers=resp_headers,
        )
    return cache_decision, JSONResponse(
        content=text_response_fn(cache_hit.response_text, route.litellm_model),
        headers=resp_headers,
    )


# ── 8. CoT-E engagement (R2, 2026-05-09) ───────────────────────────────────


async def maybe_engage_cot(
    *,
    route,
    stream: bool,
    db: AsyncSession,
    key_record,
    hint,
    body: dict,
    messages_list: list[dict],
    extra: dict,
    x_cot_iterations: Optional[str],
    x_cot_verify: Optional[str],
    x_cot_samples: Optional[str],
    x_cot_mode: Optional[str],
    x_session_id: Optional[str],
    resp_headers: dict,
    stream_cot_fn,                # callable returning an async generator
    extra_kwargs_for_stream: Optional[dict] = None,
    llm_hint: Optional[str] = None,  # only consumed by stream_cot_anthropic
):
    """Run the Chain-of-Thought-Emulation engagement pipeline.

    Pre-R2 (2026-05-09) this 42-line block was 80%-duplicated between
    ``messages.py`` and ``completions.py``. The orchestration (header
    parsing, task-branch selection, cross-provider critique pick,
    StreamingResponse construction) is identical; only the
    ``_stream_cot_*`` function differs by wire format.

    Caller passes ``stream_cot_fn`` (the shape-specific stream generator)
    and any wire-format-specific extras via ``extra_kwargs_for_stream``
    (Anthropic flow passes ``requested_model`` + ``llm_hint`` here;
    OpenAI flow passes nothing).

    Behavior:
      - Returns ``None`` when ``route.cot_engaged`` is False (caller
        proceeds to non-CoT path).
      - Raises HTTP 422 when CoT is engaged but ``stream=False`` (CoT-E
        is streaming-only by design).
      - Otherwise returns a ``StreamingResponse`` the caller should
        ``return`` directly.

    Mutates ``resp_headers`` only on engaged path.
    """
    if not route.cot_engaged:
        return None
    if not stream:
        raise HTTPException(422, "CoT-E requires stream=true")

    from fastapi.responses import StreamingResponse
    from app.cot.pipeline import parse_cot_request_headers
    from app.cot.task_adaptive import select_task_branch
    from app.routing.router import select_provider as _select_provider

    cot_max, force_verify, samples = parse_cot_request_headers(
        x_cot_iterations, x_cot_verify, x_cot_samples, x_cot_mode,
    )
    if samples > 1:
        resp_headers["X-Cot-Samples"] = str(samples)

    lmrh_task = hint.get("task").value if (hint and hint.get("task")) else None
    task_branch = select_task_branch(lmrh_task)
    if task_branch:
        resp_headers["X-Cot-Task-Branch"] = task_branch

    # Cross-provider critique (Wave 2 #8): pick a different provider for
    # the critique pass when settings.cot_cross_provider_critique is on.
    critique_model: Optional[str] = None
    critique_kwargs: Optional[dict] = None
    if settings.cot_cross_provider_critique:
        try:
            critique_route = await _select_provider(
                db, hint, has_tools=False, has_images=False,
                key_type=key_record.key_type,
                exclude_provider_id=route.provider.id,
                excluded_provider_types={"claude-oauth"},
            )
            critique_model = critique_route.litellm_model
            critique_kwargs = critique_route.litellm_kwargs
            resp_headers["X-Critique-Provider"] = critique_route.provider.name
        except Exception:
            pass  # no alternate available; critique stays on primary

    extra_kwargs = dict(extra_kwargs_for_stream or {})
    return StreamingResponse(
        stream_cot_fn(
            route.litellm_model, messages_list, x_session_id, extra,
            cot_max, route.provider.id, db, key_record.id, force_verify,
            critique_model=critique_model, critique_kwargs=critique_kwargs,
            samples=samples, task_branch=task_branch,
            **extra_kwargs,
        ),
        media_type="text/event-stream",
        headers=resp_headers,
    )
