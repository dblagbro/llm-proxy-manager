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


def _attach_bodies(metadata: dict, request_body: Any, response_body: Any) -> dict:
    """Attach captured request/response bodies to metadata when enabled."""
    if not getattr(settings, "activity_log_capture_bodies", False):
        return metadata
    cap = max(1000, int(getattr(settings, "activity_log_max_body_chars", 50000) or 50000))
    req = _serialize_body(request_body, cap)
    resp = _serialize_body(response_body, cap)
    if req is not None:
        metadata["request_body"] = req
    if resp is not None:
        metadata["response_body"] = resp
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
        msg = f"{provider_name} · {model}" if provider_name else f"{model}"
        meta = {
            "model": model,
            "provider_name": provider_name,
            "in_tok": in_tok,
            "out_tok": out_tok,
            "cost_usd": round(cost, 6),
            "latency_ms": round(latency_ms, 1),
        }
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
        msg = f"{provider_name} · {model} — error" if provider_name else f"{model} — error"
        meta = {
            "model": model,
            "provider_name": provider_name,
            "error": error_str[:2000] if error_str else None,
        }
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
