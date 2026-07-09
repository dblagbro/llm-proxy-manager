"""v5.19.0 — messages.py response-tail extract.

Pulls the four try/except blocks that fire on every non-streaming
``/v1/messages`` response into one callable. No behavior change; each
block preserves its original swallow-Exception posture (per-block failure
does not break subsequent blocks or the response return).

Blocks (in order — order matters):

1. **Capability-scout suggestion header** (v5.7.6) — emits
   ``X-Proxy-MCP-Suggestion`` when the caller's cumulative refusal-
   pattern score crosses the threshold for a suggested tool.

2. **Accept-MCP handler** (v5.12.2 + v5.16.0) — processes
   ``X-Proxy-Accept-MCP`` OR the equivalent ``accept_mcp`` key in the
   ``x-llmproxy-config`` blob. Individual header wins per v5.16.0
   precedence rule. Blob-value list is normalized to comma-separated
   string for the accept handler's expected shape.

3. **Config-applied echo header** (v5.16.0) — echoes parsed
   ``x-llmproxy-config`` back for debuggability. No-op on requests
   without the blob.

4. **Response hooks runner** (v5.14.0) — runs registered hooks with the
   full ``HookContext``. Built-in hooks today are
   ``compliance_substitution_header_hook`` and
   ``compliance_substitution_callback_hook`` (v5.18.0). Hub-side hooks
   registered via callbacks settings also fire here. Includes the
   ``compliance_event_id`` (v5.18.0) and ``substitution_reason``
   plumbing so the outbound callback emitter has the LiteLLM
   ``id`` and ``reason`` fields.

Each block is independent: an exception in one is logged (well, swallowed
today — matches pre-extract behavior) and the next block still runs.

Order rationale: (1) suggestion first because it produces a header
that downstream hooks may want to read; (2) accept before (3) echo so
the echo reflects the effective config after normalization; (4) hooks
last so they see the final resp_headers state.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._response_hook_runner import apply_response_hooks, HookContext


async def apply_response_tail(
    *,
    request: Request,
    route: Any,
    key_record: Any,
    resp_headers: dict,
    body: dict,
    db: AsyncSession,
    anthropic_result: Any = None,
) -> None:
    """Apply the post-dispatch response-shaping blocks.

    Called from ``messages.py`` immediately before ``JSONResponse``
    return. Same for the non-streaming success path only — streaming
    has its own tail in ``_messages_streaming``.

    ``anthropic_result`` is the response object (Anthropic-shape). Added
    v5.20.0 for refusal detection which needs the response text; the
    prior four blocks only touched request/route metadata.

    Never raises. Each internal block already swallows Exception; this
    wrapper preserves that posture so callers don't have to think about
    tail-block failures affecting the response.
    """
    # (0 — added v5.20.0) Refusal detection. Runs FIRST so its emitted
    # X-Refusal-Detected header is visible to any downstream hook that
    # might want to consume it. Per-key opt-in via
    # ``refusal_detection_enabled``. Default False → invisible for keys
    # that don't opt in.
    try:
        if getattr(key_record, "refusal_detection_enabled", False):
            from app.refusal_detection import (
                detect_refusal, extract_text_from_anthropic_response,
            )
            _text = extract_text_from_anthropic_response(anthropic_result)
            _match = detect_refusal(_text)
            if _match is not None:
                resp_headers["X-Refusal-Detected"] = _match.pattern_name
                resp_headers["X-Refusal-Category"] = _match.category
                # activity_log for admin dashboard + retention.
                # Non-blocking; failure to log must not affect the
                # response.
                try:
                    from datetime import datetime, timezone
                    from app.models.db import ActivityLog
                    db.add(ActivityLog(
                        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        severity="info",
                        event_type="refusal_detected",
                        api_key_id=getattr(key_record, "id", None),
                        provider_id=(
                            getattr(route.provider, "id", None)
                            if hasattr(route, "provider") else None
                        ),
                        message=(
                            f"Refusal detected: pattern={_match.pattern_name} "
                            f"category={_match.category}"
                        ),
                        event_meta={
                            "pattern_name": _match.pattern_name,
                            "category": _match.category,
                            "matched_snippet": _match.matched_snippet,
                            "requested_model": (
                                body.get("model") if isinstance(body, dict) else None
                            ),
                            "served_model": getattr(route, "litellm_model", None),
                        },
                    ))
                    await db.commit()
                except Exception:
                    pass
    except Exception:
        pass

    # (1) Capability-scout suggestion header (v5.7.6). Emits X-Proxy-MCP-
    # Suggestion when the caller's accumulated refusal-pattern score
    # crosses the threshold.
    try:
        from app.capability_scout.suggestion_emit import apply_suggestion_header
        await apply_suggestion_header(db, key_record.id, resp_headers)
    except Exception:
        pass

    # (2) Accept-MCP handler (v5.12.2 Ship 3 + v5.16.0 config blob).
    # Individual header wins over blob per the v5.16.0 precedence rule.
    try:
        from app.capability_scout.accept_handler import process_accept_header
        from app.api._llmproxy_config_header import read_config_key
        _accept_value = read_config_key(
            request, "accept_mcp", header_fallback="X-Proxy-Accept-MCP",
        )
        if _accept_value is not None:
            # Blob value may be a list; accept handler wants a
            # comma-separated string. Individual header is always a string.
            if isinstance(_accept_value, list):
                _accept_value = ",".join(str(x) for x in _accept_value)
            elif not isinstance(_accept_value, str):
                _accept_value = str(_accept_value)
            if _accept_value:
                await process_accept_header(
                    db, key_record.id, _accept_value, resp_headers,
                )
    except Exception:
        pass

    # (3) Echo parsed x-llmproxy-config back (v5.16.0). No-op on requests
    # without the blob so most traffic is unaffected.
    try:
        from app.api._llmproxy_config_header import emit_config_applied_header
        emit_config_applied_header(resp_headers, request)
    except Exception:
        pass

    # (4) Response-hooks runner (v5.14.0). Built-in hooks: substitution
    # header (v5.14.0) + substitution callback emitter (v5.18.0). Hub-
    # registered hooks also fire here. HookContext threading:
    # - compliance_event_id (v5.18.0) → LiteLLM callback ``id`` field
    # - substitution_reason  (v5.18.0) → LiteLLM callback ``reason`` field
    try:
        await apply_response_hooks(
            handler_id="messages",
            resp_headers=resp_headers,
            context=HookContext(
                requested_model=body.get("model") if isinstance(body, dict) else None,
                served_model=getattr(route, "litellm_model", None),
                api_key_id=getattr(key_record, "id", None),
                provider_id=(
                    getattr(route.provider, "id", None)
                    if hasattr(route, "provider") else None
                ),
                compliance_event_id=getattr(route, "compliance_audit_id", None),
                substituted=bool(getattr(route, "compliance_substituted", False)),
                key_record=key_record,
                request=request,
                extra={
                    "substitution_reason": getattr(
                        route, "compliance_substitution_reason", None,
                    ),
                },
            ),
        )
    except Exception:
        pass
