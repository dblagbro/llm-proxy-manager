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
