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
from app.models.db import Provider
from app.routing.circuit_breaker import (
    record_success, record_failure, is_billing_error,
    is_auth_error, classify_error, record_auth_failure, clear_auth_failure,
)
from app.monitoring.metrics import record_request
from app.monitoring.pricing import estimate_cost, estimate_cost_split
from app.monitoring.activity import log_event
from app.observability.prometheus import observe_request, observe_ttft, observe_cache_tokens
from app.routing.hedging import record_ttft_sample
from app.budget.tracker import record_cost


# v3.0.50: provider_types whose calls consume a flat-rate subscription
# quota rather than per-call billing. estimate_cost() returns the
# litellm-rate value for these calls (paperless ai-analyzer was reading
# $7/hr inflated from cross-family-substituted gpt-4o → codex-oauth
# routes that cost the operator $0 in real money). Surface the litellm
# rate as ``quota_usd`` for visibility but record ``cost_usd=0`` so
# spending-cap enforcement and per-key totals stay accurate.
SUBSCRIPTION_TIER_PROVIDER_TYPES = frozenset({
    "codex-oauth",      # operator's flat-rate ChatGPT Plus / Codex CLI
    "claude-oauth",     # Anthropic Pro Max OAuth
    "anthropic-oauth",  # legacy alias if ever introduced
    # v3.2.10: grok-web (operator's grok.com Lite/Premium subscription).
    # Cost-class is subscription — no per-call billing — so traffic to
    # grok-web should record cost=$0 to total_cost_usd and quota_usd
    # gets the rated estimate for visibility. Pre-v3.2.10 grok-web was
    # missing here, but it didn't matter because the dispatcher also
    # missed record_outcome entirely (separate v3.2.10 fix).
    "grok-web",
})


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


def _attach_client_ip(meta: dict) -> None:
    """v3.7.13 R5 — mutate ``meta`` to add ``client_ip`` and (when
    different) ``client_ip_inside`` from the per-request contextvar.

    Pre-R5 this 12-line try/except was inlined into BOTH branches of
    ``record_outcome`` (success + error), so every change to the IP
    capture model (v3.6.2 add, v3.6.3 LAN-egress rewrite split) had
    to land twice. One place now.

    Defensive: never raises. Failure to read the contextvar means the
    activity-log row lacks IP fields but the rest of the event still
    lands — better than a 500 on the log path.
    """
    try:
        from app.observability.request_context import (
            get_client_ip, get_client_ip_inside, get_internal_source,
        )
        client_ip = get_client_ip()
        client_ip_inside = get_client_ip_inside()
        if client_ip:
            meta["client_ip"] = client_ip
        # Only emit inside-ip when it differs from public — avoids
        # doubling storage on rows where the rewrite was a no-op.
        if client_ip_inside and client_ip_inside != client_ip:
            meta["client_ip_inside"] = client_ip_inside
        # v3.7.15 — BUG-017: tag internally-generated traffic (e.g.
        # the AI rate limiter calling its own proxy) so review sweeps
        # can exclude it and avoid amplifying their own previous calls.
        internal_source = get_internal_source()
        if internal_source:
            meta["internal_source"] = internal_source
    except Exception:
        pass


def _build_event_meta_base(
    *,
    model: str,
    provider_name: Optional[str],
    api_key_prefix: Optional[str],
    key_record_id: str,
    is_subscription: bool,
    is_probe: bool,
    requested_model: Optional[str],
    had_lmrh_hint: bool,
    lmrh_hint_raw: Optional[str],
    lmrh_warnings: Optional[list[str]],
) -> dict:
    """v3.7.13 R5 — build the branch-agnostic activity-log meta dict.

    Both the success and error branches of ``record_outcome`` need
    the same identifying fields (provider_name, api_key_prefix +
    api_key_id, served_model with prefix stripped, cost_class,
    client_ip pair) plus the same set of optional caller-hint fields
    (requested_model, had_lmrh_hint, lmrh_hint, lmrh_warnings, probe).

    Branches add their own fields on top of the base dict returned
    here. Anything that's structurally identical between success and
    failure events lives in this helper.
    """
    # v3.0.41: served_model is the BARE slug (litellm prefix stripped)
    # so client-side substitution-detection compares apples to apples.
    served_normalized = model.split("/", 1)[1] if "/" in model else model
    meta: dict = {
        "model": model,                       # legacy — litellm-prefixed
        "served_model": served_normalized,    # v3.0.41: bare slug for fair compare
        "provider_name": provider_name,
        "api_key_prefix": api_key_prefix,     # v3.2.12: caller attribution
        "api_key_id": key_record_id,          # v3.6.2: full id for cross-table joins
        "cost_class": "subscription" if is_subscription else "per_call",
    }
    _attach_client_ip(meta)
    if requested_model:
        meta["requested_model"] = requested_model
    if had_lmrh_hint:
        meta["had_lmrh_hint"] = True
    if lmrh_hint_raw:
        meta["lmrh_hint"] = lmrh_hint_raw[:500]
    if lmrh_warnings:
        meta["lmrh_warnings"] = list(lmrh_warnings)
    if is_probe:
        meta["probe"] = True
    return meta


async def _emit_outcome_event(
    db: AsyncSession,
    *,
    is_probe: bool,
    severity: str,
    msg: str,
    meta: dict,
    provider_id: str,
    key_record_id: str,
) -> None:
    """v3.7.13 R5 — wrap the ``log_event`` call shared by both branches.

    Encapsulates the v3.3.4 event_type split (keepalive_probe vs
    llm_request) so the same gate doesn't have to live in two places.
    """
    await log_event(
        db,
        event_type="keepalive_probe" if is_probe else "llm_request",
        message=msg,
        severity=severity,
        provider_id=provider_id,
        api_key_id=key_record_id,
        metadata=meta,
    )


def _attach_bodies(metadata: dict, request_body: Any, response_body: Any) -> dict:
    """Attach captured request/response bodies + previews to metadata when enabled.

    v3.0.94: previews and full bodies are independently controlled now.
    The 2026-05-06 incident root-caused the pool exhaustion to full-body
    capture (50K-char-each rows blew up activity_log to 1 GB). Previews
    are short (240 chars each) — bounded cost — but operators still
    want to see the message-in / response-out text in the activity log.

    - ``activity_log_capture_previews`` (default True): extract a 240-
      char preview of the user message + the response text. Lightweight,
      ~500 bytes per row max. Powers the inline Activity Log display
      and the expanded "Metadata" section.
    - ``activity_log_capture_bodies`` (default False since v3.0.91):
      capture the full serialized request + response (capped to
      ``activity_log_max_body_chars``, default 4000). Heavyweight; only
      enable when actively wire-debugging. Was the cause of the
      2026-05-06 incident when set to 50000.
    """
    capture_previews = getattr(settings, "activity_log_capture_previews", True)
    capture_bodies = getattr(settings, "activity_log_capture_bodies", False)
    if not (capture_previews or capture_bodies):
        return metadata
    if capture_previews:
        # v3.0.34: extract previews FROM THE LIVE OBJECTS (pre-serialization),
        # so truncation can't break the preview's JSON parse. Frontend prefers
        # these when present and falls back to parsing the body otherwise.
        req_preview = _extract_preview(request_body)
        resp_preview = _extract_preview(response_body)
        if req_preview:
            metadata["request_preview"] = req_preview
        if resp_preview:
            metadata["response_preview"] = resp_preview
    if capture_bodies:
        cap = max(1000, int(getattr(settings, "activity_log_max_body_chars", 4000) or 4000))
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
    # v3.0.35: new self-serve diagnostic fields per DevinGPT 2026-05-01 ask.
    # All optional + backwards-compat — older callers stay working.
    requested_model: Optional[str] = None,
    had_lmrh_hint: bool = False,
    lmrh_warnings: Optional[list[str]] = None,
    # v3.0.55: capture the raw LLM-Hint header value so post-hoc cost /
    # routing diagnostics don't have to guess at what the caller sent.
    # 2026-05-04 burn diagnosis hit a wall on this — paperless's hint was
    # silently rerouting them off claude-oauth onto Vertex (economy-tier
    # mismatch) and we couldn't see the hint to confirm.
    lmrh_hint_raw: Optional[str] = None,
) -> None:
    # v3.0.50: classify provider as subscription vs per-call so paperless's
    # cost ticker (and api_keys.total_cost_usd) doesn't inflate from
    # cross-family substitutions that route to codex-oauth at $0 real cost.
    # v3.0.57: prefer the explicit Provider.cost_class column when set;
    # fall back to provider_type-based derivation for backward compat.
    try:
        provider_obj = await db.get(Provider, provider_id)
        provider_type = getattr(provider_obj, "provider_type", None) if provider_obj else None
        explicit_cost_class = getattr(provider_obj, "cost_class", None) if provider_obj else None
    except Exception:
        provider_type = None
        explicit_cost_class = None
    # v3.2.12: denormalize the caller's key prefix into event_meta so the
    # activity log is self-contained for grep + dashboard filtering.
    # Pre-v3.2.12 readers had to JOIN api_keys on api_key_id to know which
    # caller did the request. The proactive-monitoring sweep on 2026-05-09
    # mis-attributed traffic because of this gap. Probes use the magic
    # key_record_id "probe-keepalive" which has no row in api_keys; surface
    # that as a literal "probe-keepalive" prefix so probe events stay
    # filterable without a special case at every readsite.
    api_key_prefix: Optional[str] = None
    if key_record_id == "probe-keepalive":
        api_key_prefix = "probe-keepalive"
    else:
        try:
            from app.models.db import ApiKey
            key_obj = await db.get(ApiKey, key_record_id)
            api_key_prefix = (
                getattr(key_obj, "key_prefix", None) if key_obj else None
            )
        except Exception:
            api_key_prefix = None
    if explicit_cost_class in ("subscription", "per_call"):
        is_subscription = (explicit_cost_class == "subscription")
    else:
        is_subscription = provider_type in SUBSCRIPTION_TIER_PROVIDER_TYPES
    is_probe = key_record_id == "probe-keepalive"
    if success:
        latency_ms = (time.monotonic() - t0) * 1000
        # v3.4.0: get the per-direction split. Subscription providers
        # still record real cost as zero (their flat-rate quota covers
        # it) but we keep the rated split for quota_usd reporting.
        rated_in, rated_out = estimate_cost_split(model, in_tok, out_tok)
        rated_cost = rated_in + rated_out
        cost = 0.0 if is_subscription else rated_cost
        cost_in = 0.0 if is_subscription else rated_in
        cost_out = 0.0 if is_subscription else rated_out
        quota_usd = rated_cost if is_subscription else 0.0
        await record_success(provider_id)
        # v3.3.3: pass is_probe so synthetic outcomes don't pollute
        # provider_metrics aggregates that LMRHv2 reports back to
        # callers. Activity log + circuit breaker still record probes.
        # v3.4.0: pass cost_split so per-direction columns get the
        # accurate breakdown rather than the token-proportional
        # heuristic the v3.3.x fallback applies.
        await record_request(
            db, provider_id, True, in_tok, out_tok, latency_ms, cost,
            key_record_id, ttft_ms, is_probe=is_probe,
            cost_split=(cost_in, cost_out),
        )
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
        msg_prefix = "[probe] " if is_probe else ""
        msg = f"{msg_prefix}{provider_name} · {model}" if provider_name else f"{msg_prefix}{model}"
        # v3.7.13 R5 — branch-agnostic meta fields (provider/key
        # attribution, served_model, cost_class, client_ip pair,
        # optional caller-hint fields) come from a helper now.
        meta = _build_event_meta_base(
            model=model,
            provider_name=provider_name,
            api_key_prefix=api_key_prefix,
            key_record_id=key_record_id,
            is_subscription=is_subscription,
            is_probe=is_probe,
            requested_model=requested_model,
            had_lmrh_hint=had_lmrh_hint,
            lmrh_hint_raw=lmrh_hint_raw,
            lmrh_warnings=lmrh_warnings,
        )
        # Branch-specific: per-request volume + cost + latency
        meta["in_tok"] = in_tok
        meta["out_tok"] = out_tok
        meta["cost_usd"] = round(cost, 6)
        meta["latency_ms"] = round(latency_ms, 1)
        if is_subscription:
            # litellm-rate equivalent of the consumed subscription quota.
            # Useful for "what would this have cost on per-call billing"
            # reporting; not added to spending caps.
            meta["quota_usd"] = round(quota_usd, 6)
        # v3.0.71 — echo Anthropic prompt-cache token counts so cache
        # effectiveness is visible in the activity log (not just the
        # Prometheus histogram). Only emit when non-zero (most events
        # have zero — keeps event rows lean for non-cacheable workloads).
        if cache_creation:
            meta["cache_creation_input_tokens"] = int(cache_creation)
        if cache_read:
            meta["cache_read_input_tokens"] = int(cache_read)
        meta = _attach_bodies(meta, request_body, response_body)
        await _emit_outcome_event(
            db, is_probe=is_probe, severity="info", msg=msg, meta=meta,
            provider_id=provider_id, key_record_id=key_record_id,
        )
    else:
        # v2.7.8 BUG-002: classify the error. Auth errors are PERMANENT
        # (admin must re-key) — record them in a separate map and open the
        # breaker for 24h so we stop re-trying the broken provider.
        if is_auth_error(error_str):
            await record_auth_failure(provider_id, error_str)
        else:
            await record_failure(provider_id, billing_error=is_billing_error(error_str))
        # v3.3.3: same is_probe gate on the failure path so probe-only
        # rate-limit warnings don't drag down user-visible success_rate.
        await record_request(
            db, provider_id, False, 0, 0, 0, 0, key_record_id,
            is_probe=is_probe,
        )
        observe_request(
            provider=provider_id, model=model, endpoint=endpoint,
            success=False, duration_sec=0.0,
            in_tokens=0, out_tokens=0, cost_usd=0.0,
        )
        msg_prefix = "[probe] " if is_probe else ""
        msg = (f"{msg_prefix}{provider_name} · {model} — error"
               if provider_name else f"{msg_prefix}{model} — error")
        # v3.7.13 R5 — same branch-agnostic meta as the success path.
        meta = _build_event_meta_base(
            model=model,
            provider_name=provider_name,
            api_key_prefix=api_key_prefix,
            key_record_id=key_record_id,
            is_subscription=is_subscription,
            is_probe=is_probe,
            requested_model=requested_model,
            had_lmrh_hint=had_lmrh_hint,
            lmrh_hint_raw=lmrh_hint_raw,
            lmrh_warnings=lmrh_warnings,
        )
        # Branch-specific: error blob + classified taxonomy
        # v3.0.75 — coarse error-class taxonomy for activity-log
        # filtering: auth / billing / rate_limit / timeout / network /
        # upstream_5xx / bad_request / unknown. Lets ops answer "are
        # timeout errors spiking?" without grepping the error blob.
        meta["error"] = error_str[:2000] if error_str else None
        meta["error_class"] = classify_error(error_str or "")
        meta = _attach_bodies(meta, request_body, response_body)
        await _emit_outcome_event(
            db, is_probe=is_probe, severity="warning", msg=msg, meta=meta,
            provider_id=provider_id, key_record_id=key_record_id,
        )
