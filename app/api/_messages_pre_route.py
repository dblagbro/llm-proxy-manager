"""v5.7.18 (refactor) — pre-route helpers extracted from ``messages.py``.

The /v1/messages handler grew to 1180 LOC as a single function. Per
the 2026-06-17 refactor proposal, this module hosts the three
behavior-preserving sub-block extracts from the messages() handler:

- ``prepare_request_context`` — verify API key, set tenant context,
  run compliance UA pre-check + LLM emergency stop, fire telemetry
  counter, set caller-memory request contextvars.
- ``normalize_request_body`` — input validation + suffix parsing +
  embedding-on-chat guard + ``model: "auto"`` resolution (Phase 1
  sub-block 2; lands after #1 soaks).
- ``adapt_wire_format`` — cross-family fallback body rewrite +
  Anthropic↔OpenAI translation (Phase 1 sub-block 3).

Each helper preserves the inline behavior exactly — it raises the
same HTTPException with the same args at the same time, mutates the
same dict shapes, fires the same telemetry. Source-grep pins in
``test_v5718_messages_extract.py`` confirm the call sites moved to
this module.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def prepare_request_context(
    request: Request,
    db: AsyncSession,
    x_api_key: Optional[str],
    *,
    x_conversation_id: Optional[str],
    x_memory_tag: Optional[str],
):
    """v5.7.18 — pre-body-parse setup for /v1/messages.

    Behavior is IDENTICAL to the inline version it replaces (formerly
    lines 90-140 of messages.py). Order matters: the compliance UA
    pre-check fires BEFORE the LLM emergency stop, both fire BEFORE
    any provider routing.

    Side effects:
      - Sets the per-request tenant contextvar
      - Raises HTTPException(451) for banned client UA
      - Raises HTTPException(503) when the global LLM stop is engaged
      - Increments the CONVERSATION_ID_REQUESTS_TOTAL Prometheus counter
      - Sets caller-memory presence contextvars for the activity_log row

    Returns: ``key_record`` (the verified api_keys row). The handler
    uses this for routing key_type, policy, and downstream audit.
    """
    from app.auth.keys import verify_api_key
    key_record = await verify_api_key(db, x_api_key)

    # v3.0.45: set tenant context for select_provider's ownership filter
    # so cascade/critique/hedge/grader paths inherit it without plumbing.
    from app.routing.tenant import current_api_key_id
    current_api_key_id.set(key_record.id)

    # v5.0.0 — compliance UA pre-check (decision 16 + 22). Fires BEFORE
    # any provider routing so banned client products are refused even
    # when the requested model is allowed. v5.0.9 — single mirror in
    # ``_compliance_handler.raise_if_banned_client_ua``.
    from app.api._compliance_handler import raise_if_banned_client_ua
    await raise_if_banned_client_ua(request, db, key_record)

    # v5.2.0 / Batch V1 — LLM emergency stop. Fires BEFORE provider
    # selection so a halted fleet doesn't waste a select_provider call.
    # Separate from the v5.1.0 ``activity_logging_enabled`` toggle:
    # that one suppresses log WRITES; this one refuses LLM CALLS.
    from app.api._compliance_handler import raise_if_llm_emergency_stopped
    await raise_if_llm_emergency_stopped(db, key_record, endpoint="messages")

    # v4.4.15 (F-OBS-003) — telemetry counter for caller-memory header
    # presence so the operator can see when consumers start sending it
    # (caller_memory write-back has had 0 prod writes despite the flag
    # being ON since 2026-05-15).
    try:
        from app.observability.prometheus import CONVERSATION_ID_REQUESTS_TOTAL
        CONVERSATION_ID_REQUESTS_TOTAL.labels(
            endpoint="messages",
            has_conversation_id="true" if x_conversation_id else "false",
        ).inc()
    except Exception:
        pass  # telemetry must never break the request path

    # v4.4.23 — per-request contextvars so the activity_log row can
    # record verifiable header presence (counter-only telemetry resets
    # on restart; the contextvar bridges to per-row).
    try:
        from app.observability.request_context import set_caller_memory_headers
        set_caller_memory_headers(
            has_conversation_id=bool(x_conversation_id),
            has_memory_tag=bool(x_memory_tag),
        )
    except Exception:
        pass

    return key_record


async def normalize_request_body(
    body: dict,
    x_webhook_url: Optional[str],
    db: AsyncSession,
):
    """v5.7.19 — Phase 1 sub-block 2: input validation + model
    normalization + auto-resolution. Behavior-identical to the inline
    block it replaces (formerly lines 113-259 of messages.py, the
    scattered model-normalization concerns).

    Bundles the v5.0.6 original-model capture, v3.5.8 input
    validation, v2.8.0 suffix parsing (:floor / :nitro / :exacto),
    v3.0.27 embedding-on-chat guard, and the v2.8.0 ``model: "auto"``
    resolution into one helper. Each individual concern still raises
    its own HTTPException at the same precondition point.

    Returns: ``(body, _orig_request_model, parsed_slug, is_auto, alias)``
    The handler unpacks; the body may have been rebound when the
    suffix-strip rebuild fired.
    """
    # v5.0.6 — capture the caller's ORIGINAL model name before any
    # rewriting downstream so the audit row + X-Compliance-Requested-Model
    # header carry the request as the caller sent it, not after
    # v3.0.36 cross-family rewriting.
    _orig_request_model = body.get("model") if isinstance(body, dict) else None

    # v3.5.8 BUG-005 fix — validate request shape at the input boundary.
    # Empty body → 400; missing model → 400; invalid role → 400;
    # negative max_tokens → 400. All BEFORE upstream dispatch — closes
    # the denial-of-wallet path if any API key leaks.
    from app.api._input_validation import (
        validate_completion_request,
        validate_webhook_url,
    )
    validate_completion_request(body, endpoint="messages")
    validate_webhook_url(x_webhook_url)

    # v2.8.0: parse :floor / :nitro / :exacto suffix off the requested
    # model. The suffix never reaches upstream — Anthropic / OpenAI
    # would 4xx on it.
    from app.routing.model_slug import parse_model_slug, is_auto_model
    parsed_slug = parse_model_slug(body.get("model"))
    if parsed_slug.sort_mode is not None:
        body = {**body, "model": parsed_slug.bare_model}

    # v3.0.27: same embedding-on-chat guard as completions.py —
    # embedding models can't dispatch through /v1/messages either.
    from app.routing.router import _is_embedding_model
    from fastapi import HTTPException
    if _is_embedding_model(parsed_slug.bare_model):
        raise HTTPException(
            400,
            f"Model {parsed_slug.bare_model!r} is an embeddings model. "
            f"Use POST /v1/embeddings instead of /v1/messages.",
        )

    # v2.8.0: ``model: "auto"`` / ``"llmp-auto"`` — let LMRH ranking
    # pick the provider AND the model. The auto-task classifier runs
    # later in build_hint_with_auto_task; capability scoring has
    # signal even without an explicit hint header.
    is_auto = is_auto_model(parsed_slug.bare_model)
    from app.routing.aliases import resolve_alias
    alias = await resolve_alias(db, body.get("model")) if not is_auto else None

    return body, _orig_request_model, parsed_slug, is_auto, alias


def translate_to_openai_if_needed(
    *,
    body: dict,
    route,
    system,
    messages_list: list,
    tools,
    has_tool_blocks: bool,
    has_images: bool,
) -> tuple[dict, object, list, object, bool]:
    """v5.7.20 — Phase 1 sub-block 3: Anthropic→OpenAI body translation.

    Behavior-identical to the inline block it replaces (formerly
    lines 332-386 of messages.py).

    The /v1/messages endpoint always receives an Anthropic-wire body,
    but litellm's request API is OpenAI-shaped for EVERY provider it
    dispatches (Gemini, OpenAI, OpenRouter, even litellm-Anthropic).
    Anthropic content blocks (tool_use / tool_result / image) 400 with
    "Invalid user message at index N" whenever they reach a litellm
    provider untranslated.

    v3.9.1's original Fix B only translated on cross_family_fallback,
    which left direct /v1/messages → Gemini / → OpenRouter tool-using
    requests broken (~69% of all 2026-05-15 audit warnings). v3.10.0
    widened: translation must run for ANY litellm-dispatched route
    whose body has content blocks, not just fallbacks.

    Skipped for:
      - claude-oauth (native-Anthropic dispatcher)
      - tool-emulation (its own Anthropic-shape prompt path —
        translating here would feed OpenAI-shape messages to
        normalize_anthropic_messages)

    BUG-047 (v4.3.8): also fire translation when the request carries
    Anthropic-shape tool DEFINITIONS at the top level (body.tools) but
    no tool-use/tool-result message blocks yet (first turn).

    Returns ``(body, system, messages_list, tools, translated)``. When
    ``translated`` is False, the inputs pass through unchanged.
    """
    from app.routing.tool_content import has_anthropic_tool_defs
    from app.api._oauth_chat_translate import anthropic_to_openai_body
    has_anthropic_tool_defs_flag = has_anthropic_tool_defs(body.get("tools"))
    needs_translation = (
        route.profile.provider_type != "claude-oauth"
        and not route.tool_emulation_engaged
        and (
            route.cross_family_fallback
            or has_tool_blocks
            or has_images
            or has_anthropic_tool_defs_flag
        )
    )
    if not needs_translation:
        return body, system, messages_list, tools, False

    translated = anthropic_to_openai_body({
        **body,
        "messages": messages_list,
        "system": system,
        "tools": tools,
    })
    messages_list = translated.get("messages") or []
    # Folded into messages_list as the leading role:system
    system = None
    tools = translated.get("tools")
    body = {**body, "messages": messages_list}
    body.pop("system", None)
    if tools is not None:
        body["tools"] = tools
    else:
        body.pop("tools", None)
    return body, system, messages_list, tools, True
