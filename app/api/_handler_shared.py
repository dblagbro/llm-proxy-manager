"""v5.7.23 (refactor Phase 2) — handler helpers shared between
``/v1/messages`` and ``/v1/chat/completions``.

Phase 1 (v5.7.18 + v5.7.19) extracted three sub-blocks from
``messages.py`` into ``_messages_pre_route.py``. The first two of
those — request context setup (verify key, tenant ctx, compliance
UA pre-check, LLM emergency stop, telemetry) and request body
normalization (validation, suffix parsing, embedding guard, alias
resolve) — were ALREADY repeated almost verbatim in
``completions.py``. Phase 2 lifts them here, parameterized by
``endpoint`` so each handler reuses the same logic.

What stays in ``_messages_pre_route.py``:
- ``translate_to_openai_if_needed`` — Anthropic→OpenAI body
  translation, only meaningful for ``/v1/messages``. The OpenAI
  endpoint receives OpenAI-wire bodies natively and never runs this.

Sub-block #3 of Phase 1 (the Anthropic→OpenAI translation) is the
narrowest concern; co-locating it with the rest of the pre-route
work in ``_messages_pre_route.py`` would put endpoint-specific
logic in a "shared" module. The split here keeps boundaries clean.
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
    endpoint: str,
    x_conversation_id: Optional[str],
    x_memory_tag: Optional[str],
):
    """v5.7.23 — shared pre-body-parse setup for /v1/messages and
    /v1/chat/completions.

    Behavior is IDENTICAL to the v5.7.18 ``_messages_pre_route``
    version it supersedes, with ONE change: the ``endpoint`` kwarg is
    now required and propagates to both the compliance LLM-stop check
    and the Prometheus counter label. Callers:

      - messages.py:    ``endpoint="messages"``
      - completions.py: ``endpoint="completions"``

    The compliance UA pre-check fires first (raises 451), then the
    LLM emergency stop (raises 503), then telemetry. Order matters —
    a banned client must be refused BEFORE we tally a request.

    Returns: ``key_record`` (the verified api_keys row).
    """
    from app.auth.keys import verify_api_key
    key_record = await verify_api_key(db, x_api_key)

    # v3.0.45: tenant context for select_provider's ownership filter
    # so cascade/critique/hedge/grader paths inherit it without plumbing.
    from app.routing.tenant import current_api_key_id
    current_api_key_id.set(key_record.id)

    # v5.0.0 — compliance UA pre-check (decision 16 + 22). Refuse banned
    # client products BEFORE any provider routing.
    from app.api._compliance_handler import raise_if_banned_client_ua
    await raise_if_banned_client_ua(request, db, key_record)

    # v5.2.0 / Batch V1 — LLM emergency stop. Refuses LLM CALLS when
    # the global stop is engaged. Separate from the v5.1.0
    # ``activity_logging_enabled`` toggle (which suppresses log WRITES).
    from app.api._compliance_handler import raise_if_llm_emergency_stopped
    await raise_if_llm_emergency_stopped(db, key_record, endpoint=endpoint)

    # v4.4.15 (F-OBS-003) — caller-memory header presence counter.
    try:
        from app.observability.prometheus import CONVERSATION_ID_REQUESTS_TOTAL
        CONVERSATION_ID_REQUESTS_TOTAL.labels(
            endpoint=endpoint,
            has_conversation_id="true" if x_conversation_id else "false",
        ).inc()
    except Exception:
        pass  # telemetry must never break the request path

    # v4.4.23 — per-request contextvars so the activity_log row can
    # record verifiable header presence.
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
    *,
    endpoint: str,
):
    """v5.7.23 — shared input validation + model normalization for
    both /v1/messages and /v1/chat/completions.

    The validation helper itself dispatches on the ``endpoint`` arg
    (different required-field rules for OpenAI vs Anthropic shapes).
    Suffix-strip, embedding-on-chat guard, and ``model:"auto"``
    resolution are identical across endpoints.

    Returns: ``(body, _orig_request_model, parsed_slug, is_auto, alias)``.
    """
    # v5.0.6 — capture caller's ORIGINAL model name before any rewrite.
    _orig_request_model = body.get("model") if isinstance(body, dict) else None

    # v3.5.8 — input boundary validation (400 instead of 502 cascade).
    from app.api._input_validation import (
        validate_completion_request,
        validate_webhook_url,
    )
    validate_completion_request(body, endpoint=endpoint)
    validate_webhook_url(x_webhook_url)

    # v2.8.0 — parse :floor / :nitro / :exacto suffix; never reaches upstream.
    from app.routing.model_slug import parse_model_slug, is_auto_model
    parsed_slug = parse_model_slug(body.get("model"))
    if parsed_slug.sort_mode is not None:
        body = {**body, "model": parsed_slug.bare_model}

    # v3.0.27 — embedding-on-chat guard.
    from app.routing.router import _is_embedding_model
    from fastapi import HTTPException
    if _is_embedding_model(parsed_slug.bare_model):
        endpoint_path = "/v1/messages" if endpoint == "messages" else "/v1/chat/completions"
        raise HTTPException(
            400,
            f"Model {parsed_slug.bare_model!r} is an embeddings model. "
            f"Use POST /v1/embeddings instead of {endpoint_path}.",
        )

    is_auto = is_auto_model(parsed_slug.bare_model)
    from app.routing.aliases import resolve_alias
    alias = await resolve_alias(db, body.get("model")) if not is_auto else None

    return body, _orig_request_model, parsed_slug, is_auto, alias
