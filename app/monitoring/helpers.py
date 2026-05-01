"""Shared post-request outcome recorder.

Centralises the record_success/record_failure + estimate_cost + record_request
pattern that appears in every streaming and non-streaming handler.
"""
import json
import re
import time
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.routing.circuit_breaker import (
    record_success, record_failure, is_billing_error,
    is_auth_error, record_auth_failure, clear_auth_failure,
)
from app.monitoring.metrics import record_request
from app.monitoring.pricing import estimate_cost
from app.monitoring.activity import log_event
from app.observability.prometheus import observe_request, observe_ttft, observe_cache_tokens
from app.routing.hedging import record_ttft_sample
from app.budget.tracker import record_cost


# v2.8.4: redact known secret patterns in case they leak into logged bodies.
# Anyone providing an api_key in the request body, or a system prompt with a
# leaked token, gets it scrubbed before persisting.
_SECRET_PATTERNS = [
    (re.compile(r"sk-ant-(?:api|oat|ort)\d*-[\w-]+", re.I), "sk-ant-***REDACTED***"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}", re.I), "sk-***REDACTED***"),
    (re.compile(r"AIza[A-Za-z0-9_-]{35}", re.I), "AIza***REDACTED***"),
    (re.compile(r'"api_key"\s*:\s*"[^"]+"', re.I), '"api_key": "***REDACTED***"'),
    (re.compile(r'(Authorization|x-api-key)\s*:\s*[^\s",]+', re.I),
     r'\1: ***REDACTED***'),
]


def _redact(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _serialize_body(body: Any, max_chars: int) -> Optional[str]:
    """Compact-JSON serialize + redact + truncate. Returns None on failure."""
    if body is None:
        return None
    try:
        if isinstance(body, (dict, list)):
            text = json.dumps(body, ensure_ascii=False, default=str)
        else:
            text = str(body)
    except Exception:
        return None
    text = _redact(text)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "…[TRUNCATED]"
    return text


def _extract_preview(body: Any, max_chars: int = 240) -> Optional[str]:
    """v3.0.34: extract a short text preview from a request/response body
    BEFORE truncation. Frontend was falling through to raw JSON when the
    serialized body exceeded the truncation cap (50k default) — JSON.parse
    on a `…[TRUNCATED]` string throws. Storing the preview separately
    sidesteps that whole class of issue.

    For Anthropic-shape requests: last user message text (or tool_result
    summary). For Anthropic responses: content[].text. For OpenAI: choices
    [0].message.content. Falls back to a system-prompt snippet, then to the
    repr of a small body."""
    if body is None:
        return None
    try:
        d = body if isinstance(body, dict) else None
        if d is None:
            return str(body)[:max_chars]
        # Anthropic / OpenAI request: walk messages backward for last user content
        msgs = d.get("messages")
        if isinstance(msgs, list) and msgs:
            for m in reversed(msgs):
                if not isinstance(m, dict) or m.get("role") != "user":
                    continue
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    return c[:max_chars]
                if isinstance(c, list):
                    parts = []
                    for blk in c:
                        if not isinstance(blk, dict):
                            continue
                        t = blk.get("type")
                        if t == "text" and isinstance(blk.get("text"), str):
                            parts.append(blk["text"])
                        elif t == "tool_result":
                            tc = blk.get("content")
                            if isinstance(tc, str):
                                parts.append(f"[tool_result] {tc}")
                            elif isinstance(tc, list):
                                inner = " ".join(
                                    b.get("text", "") for b in tc
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                                if inner:
                                    parts.append(f"[tool_result] {inner}")
                    txt = " ".join(p for p in parts if p).strip()
                    if txt:
                        return txt[:max_chars]
        # Anthropic-shape response: content[]
        cont = d.get("content")
        if isinstance(cont, list) and cont:
            txt = " ".join(b.get("text", "") for b in cont
                           if isinstance(b, dict) and b.get("type") == "text").strip()
            if txt:
                return txt[:max_chars]
        # OpenAI-shape response: choices[0].message.content
        ch = d.get("choices")
        if isinstance(ch, list) and ch:
            mc = (ch[0] or {}).get("message", {}).get("content")
            if isinstance(mc, str) and mc.strip():
                return mc[:max_chars]
        # Last-ditch: system prompt snippet (request side).
        sysp = d.get("system")
        if isinstance(sysp, str) and sysp.strip():
            return f"[system] {sysp[:max_chars - 9]}"
        if isinstance(sysp, list):
            txt = " ".join(b.get("text", "") for b in sysp
                           if isinstance(b, dict) and b.get("type") == "text").strip()
            if txt:
                return f"[system] {txt[:max_chars - 9]}"
    except Exception:
        return None
    return None


def _attach_bodies(metadata: dict, request_body: Any, response_body: Any) -> dict:
    """Attach captured request/response bodies + previews to metadata when enabled."""
    if not getattr(settings, "activity_log_capture_bodies", False):
        return metadata
    cap = max(1000, int(getattr(settings, "activity_log_max_body_chars", 50000) or 50000))
    # v3.0.34: extract previews FROM THE LIVE OBJECTS (pre-serialization), so
    # truncation can't break the preview's JSON parse. Frontend prefers these
    # when present and falls back to parsing the body otherwise.
    req_preview = _extract_preview(request_body)
    resp_preview = _extract_preview(response_body)
    req = _serialize_body(request_body, cap)
    resp = _serialize_body(response_body, cap)
    if req is not None:
        metadata["request_body"] = req
    if resp is not None:
        metadata["response_body"] = resp
    if req_preview:
        metadata["request_preview"] = req_preview
    if resp_preview:
        metadata["response_preview"] = resp_preview
    return metadata


async def record_outcome(
    db: AsyncSession,
    provider_id: str,
    model: str,
    *,
    success: bool,
    in_tok: int = 0,
    out_tok: int = 0,
    t0: float = 0.0,
    key_record_id: str,
    error_str: str = "",
    ttft_ms: float = 0.0,
    endpoint: str = "messages",
    cache_creation: int = 0,
    cache_read: int = 0,
    request_body: Any = None,
    response_body: Any = None,
    provider_name: Optional[str] = None,
) -> None:
    if success:
        latency_ms = (time.monotonic() - t0) * 1000
        cost = estimate_cost(model, in_tok, out_tok)
        await record_success(provider_id)
        await record_request(db, provider_id, True, in_tok, out_tok, latency_ms, cost, key_record_id, ttft_ms)
        await record_cost(db, key_record_id, cost)
        observe_request(
            provider=provider_id, model=model, endpoint=endpoint,
            success=True, duration_sec=latency_ms / 1000.0,
            in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost,
        )
        if ttft_ms > 0:
            observe_ttft(provider_id, model, ttft_ms / 1000.0)
            record_ttft_sample(provider_id, ttft_ms)
        if cache_creation or cache_read:
            observe_cache_tokens(provider_id, model, cache_creation, cache_read)
        # v2.7.8 BUG-002: a successful call clears any prior auth_failed flag —
        # whatever revoked the key is fixed (admin re-keyed, OAuth refreshed, etc.)
        clear_auth_failure(provider_id)
        # v2.8.5: human-friendly message — use provider_name when given.
        # Reads e.g. "Devin-VG · claude-sonnet-4-6" instead of just "claude-oauth".
        is_probe = key_record_id == "probe-keepalive"
        msg_prefix = "[probe] " if is_probe else ""
        msg = f"{msg_prefix}{provider_name} · {model}" if provider_name else f"{msg_prefix}{model}"
        meta = {
            "model": model,
            "provider_name": provider_name,
            "in_tok": in_tok,
            "out_tok": out_tok,
            "cost_usd": round(cost, 6),
            "latency_ms": round(latency_ms, 1),
        }
        if is_probe:
            meta["probe"] = True
        meta = _attach_bodies(meta, request_body, response_body)
        await log_event(
            db,
            event_type="llm_request",
            message=msg,
            severity="info",
            provider_id=provider_id,
            api_key_id=key_record_id,
            metadata=meta,
        )
    else:
        # v2.7.8 BUG-002: classify the error. Auth errors are PERMANENT
        # (admin must re-key) — record them in a separate map and open the
        # breaker for 24h so we stop re-trying the broken provider.
        if is_auth_error(error_str):
            await record_auth_failure(provider_id, error_str)
        else:
            await record_failure(provider_id, billing_error=is_billing_error(error_str))
        await record_request(db, provider_id, False, 0, 0, 0, 0, key_record_id)
        observe_request(
            provider=provider_id, model=model, endpoint=endpoint,
            success=False, duration_sec=0.0,
            in_tokens=0, out_tokens=0, cost_usd=0.0,
        )
        is_probe = key_record_id == "probe-keepalive"
        msg_prefix = "[probe] " if is_probe else ""
        msg = (f"{msg_prefix}{provider_name} · {model} — error"
               if provider_name else f"{msg_prefix}{model} — error")
        meta = {
            "model": model,
            "provider_name": provider_name,
            "error": error_str[:2000] if error_str else None,
        }
        if is_probe:
            meta["probe"] = True
        meta = _attach_bodies(meta, request_body, response_body)
        await log_event(
            db,
            event_type="llm_request",
            message=msg,
            severity="warning",
            provider_id=provider_id,
            api_key_id=key_record_id,
            metadata=meta,
        )
