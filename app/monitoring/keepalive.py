"""Per-provider keep-alive probes (v3.0.2).

Sends one cheap synthetic call to each enabled provider on a configurable
interval (default every 5 min) so the activity log + provider_metrics +
dashboards reflect liveness regardless of organic traffic.

Probe payload is unique per provider so a glance at activity_log shows
who answered:

    user: "Hi from Devin-VG"  →  assistant: "Hello, …"

Each probe is logged via the existing ``record_outcome()`` path, so:
  - activity_log gets an ``llm_request`` row with ``probe: true`` in
    event_meta (UI can filter these out of cost dashboards if desired)
  - provider_metrics buckets see a +1 request, success/failure tracked
  - circuit_breaker state updates from the probe's success/failure
  - cost is computed via the same estimate_cost path so $0.0001-ish
    landings show up where token counts are non-zero

Skip rules:
  - Disabled / soft-deleted providers
  - claude-oauth providers — their dispatch path doesn't go through
    litellm.acompletion; probing them needs the OAuth handler. Future
    work; for now they're skipped to avoid burning OAuth refresh tokens.
  - Providers that received any real traffic in the last 2× probe
    interval — no point burning budget when traffic is flowing
"""
from __future__ import annotations

import asyncio
import httpx
import logging
import time
from typing import Optional

import litellm
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import Provider, ProviderMetric
from app.models.database import AsyncSessionLocal
from app.monitoring.helpers import record_outcome

logger = logging.getLogger(__name__)


_DEFAULT_INTERVAL_SEC = 300         # 5 min
_PROBE_TIMEOUT_SEC = 15
_PROBE_MAX_TOKENS = 8


def _probe_interval_sec() -> int:
    """Admin-tunable; 0 disables probes globally."""
    try:
        v = int(getattr(settings, "keepalive_probe_interval_sec", _DEFAULT_INTERVAL_SEC))
        return max(0, v)
    except Exception:
        return _DEFAULT_INTERVAL_SEC


# v3.3.3: per-provider rate-limit back-off state.
# When a probe gets a rate_limit error (HTTP 429), we delay the next
# probe by interval × factor^N where N is consecutive 429s (capped at
# backoff_max_sec). Resets on success. Pre-v3.3.3 every probe fired
# every 5min unconditionally — when grok.com rate-limited us, the
# next probe re-hit the same window and the cycle continued for hours.
_probe_backoff_until: dict[str, float] = {}
# v5.17.1 — separate cache for chronic-CB-open gating (see sweep).
_chronic_backoff_until: dict[str, float] = {}
_consecutive_rate_limits: dict[str, int] = {}


def _is_rate_limit_error(error_str: str) -> bool:
    """Lightweight rate-limit detector. Mirrors the rate_limit-class
    patterns from circuit_breaker.classify_error() but without the
    full taxonomy import to keep the back-off path cheap."""
    if not error_str:
        return False
    low = error_str.lower()
    return any(p in low for p in (
        "429", "rate_limit", "rate limit", "too many requests",
        "ratelimit", "throttled",
    ))


# v5.9.1 — consecutive probe auth-failure streak. Re-persists
# auto_skip_until after threshold so the v5.8.6/7 gates re-engage on
# permanently-dead refresh_tokens whose auto_skip_until has expired.
_PROBE_AUTH_FAILURE_STREAK: dict[str, int] = {}
_PROBE_AUTH_FAILURE_RE_SKIP_THRESHOLD = 10


def _looks_like_auth_failure(error_str: str) -> bool:
    """Pattern match — true when the probe error implies persistent
    auth failure (revoked token, missing scopes, 401). Tolerates
    classifier drift by listing canonical OAuth refusal phrases.
    """
    if not error_str:
        return False
    low = error_str.lower()
    return any(p in low for p in (
        "401", "unauthorized", "invalid authentication",
        "invalid_grant", "invalid_token", "missing scopes",
        "refresh_token_reused", "refresh_token_expired",
        "needs_reauth", "needs re-auth", "permission denied",
    ))


def _backoff_skip(provider_id: str) -> bool:
    """True if this provider is currently in a rate-limit back-off
    window and should be skipped this sweep."""
    until = _probe_backoff_until.get(provider_id)
    if not until:
        return False
    return time.time() < until


def _record_probe_outcome_for_backoff(provider_id: str, error_str: str) -> None:
    """Update back-off state after a probe completes. Called from
    _probe_one() so all probe paths feed it.

    On rate-limit error: increment consecutive counter, set
    _probe_backoff_until = now + interval × factor^N (capped at max).
    On any other outcome (success, auth error, etc.): clear state so
    the next probe fires on the normal cadence."""
    if _is_rate_limit_error(error_str):
        n = _consecutive_rate_limits.get(provider_id, 0) + 1
        _consecutive_rate_limits[provider_id] = n
        try:
            interval = _probe_interval_sec() or _DEFAULT_INTERVAL_SEC
            factor = float(getattr(
                settings, "keepalive_probe_rate_limit_backoff_factor", 2.0,
            ))
            cap = int(getattr(
                settings, "keepalive_probe_rate_limit_backoff_max_sec", 1800,
            ))
        except Exception:
            interval, factor, cap = _DEFAULT_INTERVAL_SEC, 2.0, 1800
        if cap <= 0 or factor <= 1.0:
            # Back-off disabled — clear any existing window.
            _probe_backoff_until.pop(provider_id, None)
            return
        # delay = interval × factor^N, capped. N=1 → interval×factor;
        # the regular loop sleep will already wait one interval, so
        # the first 429 doubles the gap (interval×2 from now ≈
        # interval since the loop sleeps interval before the next
        # sweep anyway → effective ≥ 2×interval = 10min by default).
        delay = min(interval * (factor ** n), float(cap))
        _probe_backoff_until[provider_id] = time.time() + delay
        logger.info(
            "keepalive.backoff_set provider=%s consecutive_429=%d "
            "next_probe_in_sec=%.0f",
            provider_id, n, delay,
        )
    else:
        # Any non-rate-limit outcome (success, auth, network) resets.
        if provider_id in _consecutive_rate_limits:
            _consecutive_rate_limits.pop(provider_id, None)
            _probe_backoff_until.pop(provider_id, None)


def get_backoff_state() -> dict[str, dict]:
    """Diagnostic snapshot — used by admin endpoints + tests to inspect
    which providers are currently throttled and for how long."""
    now = time.time()
    return {
        pid: {
            "consecutive_rate_limits": _consecutive_rate_limits.get(pid, 0),
            "backoff_remaining_sec": max(0.0, until - now),
        }
        for pid, until in _probe_backoff_until.items()
    }


async def _had_recent_traffic(db: AsyncSession, provider_id: str, lookback_sec: int) -> bool:
    """True if any provider_metrics bucket for this provider was updated
    within the lookback window. Cheaper than scanning activity_log."""
    cutoff = func.datetime("now", f"-{lookback_sec} seconds")
    res = await db.execute(
        select(func.count(ProviderMetric.id)).where(
            ProviderMetric.provider_id == provider_id,
            ProviderMetric.bucket_ts >= cutoff,
            ProviderMetric.requests > 0,
        )
    )
    return (res.scalar() or 0) > 0


async def _probe_one(provider: Provider) -> None:
    """Send one synthetic call to a provider. All errors swallowed —
    keep-alive is best-effort, doesn't block routing.

    v5.19.1 — chronic-CB gate moved here from the sweep loop. Both call
    paths now honor it: the sweep loop calls this after its own filters,
    AND the CB's auto-probe path (circuit_breaker._auto_probe) calls
    this directly. v5.17.1 gated only the sweep, so the CB auto-probe
    kept slipping through and re-cycling Grok every ~65 min. Fix: gate
    at the choke point instead of the caller.
    """
    # v5.19.1 — chronic-CB gate. Same logic as v5.17.1 sweep gate but
    # applied here so ALL callers benefit. When consecutive_opens >= N
    # (default 5) AND the backoff window is still active, drop this
    # probe. Backoff is per-provider, 6h by default.
    from app.routing.circuit_breaker import get_consecutive_opens
    _co = get_consecutive_opens(provider.id)
    _co_threshold = getattr(settings, "keepalive_chronic_cb_open_threshold", 5)
    if _co >= _co_threshold:
        _co_backoff_sec = getattr(
            settings, "keepalive_chronic_cb_open_backoff_sec", 21600,
        )
        _key = f"chronic_cb_{provider.id}"
        _now = time.time()
        _next = _chronic_backoff_until.get(_key, 0.0)
        if _next == 0.0:
            _chronic_backoff_until[_key] = _now + _co_backoff_sec
            logger.info(
                "keepalive.chronic_cb_gated provider=%s consecutive_opens=%d "
                "backoff_sec=%d (v5.19.1 — at _probe_one)",
                provider.id, _co, _co_backoff_sec,
            )
            return
        if _now < _next:
            logger.debug(
                "keepalive.skipped_chronic_cb_gated provider=%s remaining=%.0fs",
                provider.id, _next - _now,
            )
            return
        # Backoff elapsed — clear + let this probe fire to detect recovery.
        _chronic_backoff_until.pop(_key, None)

    # v3.0.32: shared helper resolves chat-capable model when default is
    # embedding-only. Replaces inline logic that was previously duplicated
    # here and in scanner.test_provider — see resolve_chat_model_for_provider
    # docstring for the bug history.
    from app.routing.router import resolve_chat_model_for_provider
    async with AsyncSessionLocal() as _resolve_db:
        chat_model, skip_reason = await resolve_chat_model_for_provider(
            _resolve_db, provider
        )
    if skip_reason is not None:
        logger.debug("keepalive.skipped_embedding_only provider=%s reason=%s",
                     provider.id, skip_reason)
        return
    model = chat_model or "gpt-4o-mini"
    # Build litellm-shape model id from provider_type if no slash
    if "/" not in model:
        if provider.provider_type in ("anthropic",):
            litellm_model = f"anthropic/{model}"
        elif provider.provider_type in ("openai", "compatible"):
            litellm_model = f"openai/{model}"
        elif provider.provider_type in ("google", "vertex"):
            litellm_model = f"gemini/{model}"
        elif provider.provider_type == "grok":
            litellm_model = f"xai/{model}"
        else:
            litellm_model = model
    else:
        litellm_model = model

    prompt = f"Hi from {provider.name}"
    t0 = time.monotonic()
    success = False
    in_tok = out_tok = 0
    err_str = ""

    if provider.provider_type == "claude-oauth":
        # OAuth providers use a different auth path (Bearer + CC beta flags
        # via platform.claude.com). Reuse the dispatch helper from messages.py
        # rather than going through litellm.
        from app.api._messages_streaming import _complete_claude_oauth
        # v5.0.21 — per-provider 1M-context opt-out via ContextVar
        # v5.0.21 hotfix: defensive getattr + identity-check for bool.
        from app.providers.claude_oauth import set_disable_long_context
        set_disable_long_context(
            (getattr(provider, "extra_config", None) or {}).get("disable_long_context") is True
        )
        try:
            async with AsyncSessionLocal() as _oauth_db:
                resp = await asyncio.wait_for(
                    _complete_claude_oauth(
                        provider.api_key,
                        body={
                            "model": model,
                            "max_tokens": _PROBE_MAX_TOKENS,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                        provider_id=provider.id,
                        db=_oauth_db,
                        key_record_id="probe-keepalive",
                        t0=t0,
                        provider_name=provider.name,
                    ),
                    timeout=_PROBE_TIMEOUT_SEC,
                )
            # _complete_claude_oauth itself calls record_outcome, so we
            # don't double-log; this branch returns early.
            return
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e) or 'no message'}"
            # Fall through to the generic record_outcome path below so the
            # error gets logged with probe markers.
    elif provider.provider_type == "grok-web":
        # v3.2.10: grok-web probes use the dispatcher's complete_grok_web
        # path (manual or bridge mode, depending on extra_config). On
        # success / failure we fall through to the generic record_outcome
        # block below — same shape as the other branches. The bridge
        # path also exercises Cloudflare cookie freshness as a side
        # effect, so a probe failure here is the earliest signal that
        # the operator's session needs re-login.
        from app.providers.grok_web import (
            complete_grok_web, GrokWebError, GrokWebAuthError,
        )
        try:
            resp = await asyncio.wait_for(
                complete_grok_web(
                    provider.extra_config or {},
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    timeout=float(_PROBE_TIMEOUT_SEC),
                ),
                timeout=float(_PROBE_TIMEOUT_SEC),
            )
            success = True
            usage = resp.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens") or 0)
            out_tok = int(usage.get("completion_tokens") or 0)
        except (GrokWebAuthError, GrokWebError) as e:
            err_str = f"{type(e).__name__}: {str(e)[:200]}"
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e) or 'no message'}"
        litellm_model = model  # for the activity_log message string below
        # v4.4 M-3 — record this node's view of the bridge into
        # ProviderNodeAuthState. The probe is the canonical "can this
        # node serve grok-web right now?" signal; mapping its outcome
        # to a per-(provider_id, node_id) auth_state row gives M-4's
        # routing filter + M-5's UI the data they need without adding
        # a new endpoint or HTTP hop. Best-effort: any failure here
        # is swallowed so it doesn't corrupt the probe's own
        # record_outcome path below.
        try:
            from app.routing import node_auth_state as _nas
            if success:
                _new_state = "ok"
                _last_error = None
            else:
                # Classify the probe error into an auth_state. Use the
                # existing circuit_breaker classifier (BUG-048 widened
                # it for grok-bridge prose) to decide between
                # "needs_reauth" (auth/bad_request — operator action)
                # and "bridge_down" (network/timeout/upstream_5xx —
                # transient).
                from app.routing.circuit_breaker import classify_error
                _cls = classify_error(err_str or "")
                if _cls == "auth":
                    _new_state = "needs_reauth"
                elif _cls in ("network", "timeout", "upstream_5xx", "rate_limit"):
                    # rate_limit added v4.4.1 (BUG-051): a 429 is
                    # transient throttling, not a re-auth signal.
                    # Self-clears on the next successful probe.
                    _new_state = "bridge_down"
                else:
                    # bad_request (e.g. "Conversation 'X' was not
                    # found"), billing, unknown → needs_reauth.
                    # Operator-time signal; routing won't pick it.
                    _new_state = "needs_reauth"
                _last_error = err_str
            async with AsyncSessionLocal() as _nas_db:
                await _nas.write_local_state(
                    _nas_db,
                    provider.id,
                    _new_state,
                    last_error=_last_error,
                )
                await _nas_db.commit()
        except Exception as _e:
            logger.debug(
                "keepalive.m3_node_auth_state_write_failed provider=%s err=%r",
                provider.id, _e,
            )
    elif provider.provider_type == "ChatGPT-oauth-plan":
        # v3.0.19: codex-oauth probes were going through litellm.acompletion
        # (openai/gpt-5.5), which routes to api.openai.com — that endpoint
        # rejects Codex CLI bearer tokens with "Missing scopes: model.request".
        # Use the direct dispatch path with the right headers + body shape.
        # Minimal inline call (rather than calling _test_codex_oauth) so the
        # standard record_outcome path below logs the activity_log entry
        # with the right probe markers without double-recording.
        from app.providers.codex_oauth import (
            CODEX_RESPONSES_URL, build_headers,
        )
        cfg = provider.extra_config or {}
        account_id = cfg.get("chatgpt_account_id") if isinstance(cfg, dict) else None
        codex_body = {
            "model": model,
            "instructions": "Reply briefly.",
            "input": [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }],
            "stream": True,
            "store": False,
        }
        try:
            headers = build_headers(provider.api_key, chatgpt_account_id=account_id)
            # v3.0.60: split connect/read so probes fail fast on DNS outages.
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=float(_PROBE_TIMEOUT_SEC), write=5.0, pool=5.0),
            ) as _c:
                async with _c.stream(
                    "POST", CODEX_RESPONSES_URL, headers=headers, json=codex_body,
                ) as _r:
                    if _r.status_code >= 400:
                        body = await _r.aread()
                        err_str = f"{_r.status_code}: {body[:300].decode(errors='replace')}"
                    else:
                        # Drain enough events to confirm response.completed.
                        # v3.0.98: also parse usage from the completed event
                        # so probe rows show non-zero in_tok/out_tok like the
                        # litellm path does. Pre-fix every codex probe row
                        # showed 0/0 because we broke out without parsing
                        # the JSON. Operator-flagged 2026-05-07.
                        import json as _json
                        async for line in _r.aiter_lines():
                            if not line.startswith("data:") or "response.completed" not in line:
                                continue
                            success = True
                            try:
                                payload = _json.loads(line[5:].strip())
                                resp_obj = payload.get("response") or payload
                                usage = resp_obj.get("usage") or {}
                                in_tok = int(usage.get("input_tokens") or 0)
                                out_tok = int(usage.get("output_tokens") or 0)
                            except Exception:
                                pass  # success=True but tokens stay at 0
                            break
                        if not success:
                            err_str = "stream ended without response.completed"
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e) or 'no message'}"
        litellm_model = model  # for the activity_log message string below
    else:
        kwargs = {"api_key": provider.api_key, "max_tokens": _PROBE_MAX_TOKENS}
        if provider.base_url:
            kwargs["api_base"] = provider.base_url

        try:
            resp = await asyncio.wait_for(
                litellm.acompletion(
                    model=litellm_model,
                    messages=[{"role": "user", "content": prompt}],
                    **kwargs,
                ),
                timeout=_PROBE_TIMEOUT_SEC,
            )
            success = True
            try:
                in_tok = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
                out_tok = int(getattr(resp.usage, "completion_tokens", 0) or 0)
            except Exception:
                in_tok = out_tok = 0
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e) or 'no message'}"

    # Log via the standard outcome recorder so activity_log + metrics
    # + circuit-breaker all see the result. ``probe=True`` in metadata
    # lets the UI distinguish synthetic from organic.
    async with AsyncSessionLocal() as db:
        # Use a phantom api_key_id so per-key budget tracking doesn't
        # attribute probe cost to a tenant. The ``probe-keepalive``
        # value is recognised in dashboards as the synthetic source.
        try:
            await record_outcome(
                db,
                provider_id=provider.id,
                model=litellm_model,
                success=success,
                in_tok=in_tok,
                out_tok=out_tok,
                t0=t0,
                key_record_id="probe-keepalive",
                error_str=err_str,
                provider_name=provider.name,
                request_body={"_probe": True, "model": litellm_model,
                              "messages": [{"role": "user", "content": prompt}]},
                response_body=({"_probe": True, "ok": True,
                                "tokens_in": in_tok, "tokens_out": out_tok}
                               if success else None),
            )
        except Exception as e:
            logger.warning("keepalive.record_outcome_failed provider=%s err=%s",
                           provider.id, e)
    # v3.3.3: feed the back-off state machine. Success clears any
    # consecutive-429 streak; rate_limit error extends the next-probe
    # delay. Done OUTSIDE the AsyncSessionLocal block so DB lifetime
    # doesn't entangle with in-memory dict state.
    _record_probe_outcome_for_backoff(provider.id, err_str if not success else "")

    # v5.9.1 — persistent-auth-failure-via-probe streak. v5.8.3 fix #3
    # routed probe auth failures through ``record_failure`` (not
    # ``record_auth_failure``) so a single transient 401 wouldn't
    # auto_skip the provider for 24h. The unintended consequence:
    # when a provider's refresh_token is permanently revoked AND the
    # auto_skip_until set by an organic-traffic 401 eventually
    # expires, probes resume and the CB cycles indefinitely
    # (open → 120s hold-down → reopen at failures+1 → repeat). The
    # v5.8.7 keepalive gate catches it while auto_skip is set, but
    # once auto_skip expires there's no re-engagement.
    #
    # Fix: track consecutive probe auth-failures per provider; when
    # the streak crosses ``_PROBE_AUTH_FAILURE_RE_SKIP_THRESHOLD``
    # (default 10 ≈ 50min at the default 5min probe cadence), persist
    # auto_skip_until = +24h. That re-engages the v5.8.6/7 gates and
    # silences the cycle. Any success resets the streak.
    if not success and _looks_like_auth_failure(err_str):
        _PROBE_AUTH_FAILURE_STREAK[provider.id] = (
            _PROBE_AUTH_FAILURE_STREAK.get(provider.id, 0) + 1
        )
        if _PROBE_AUTH_FAILURE_STREAK[provider.id] >= _PROBE_AUTH_FAILURE_RE_SKIP_THRESHOLD:
            try:
                from app.routing.circuit_breaker import _persist_auto_skip
                await _persist_auto_skip(
                    provider.id,
                    f"persistent_auth_failure_via_probe_streak "
                    f"(n={_PROBE_AUTH_FAILURE_STREAK[provider.id]})",
                )
                logger.info(
                    "keepalive.persisted_auto_skip_via_streak provider=%s "
                    "streak=%d",
                    provider.id, _PROBE_AUTH_FAILURE_STREAK[provider.id],
                )
                # Reset so the next streak counts from 0.
                _PROBE_AUTH_FAILURE_STREAK[provider.id] = 0
            except Exception as _e:
                logger.warning(
                    "keepalive.persist_auto_skip_via_streak_failed "
                    "provider=%s err=%r", provider.id, _e,
                )
    elif success:
        _PROBE_AUTH_FAILURE_STREAK.pop(provider.id, None)


async def _probe_all_once() -> int:
    """Probe every eligible provider once. Returns count probed."""
    interval = _probe_interval_sec()
    if interval == 0:
        return 0
    skip_lookback = max(60, 2 * interval)
    count = 0
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Provider).where(
                Provider.enabled == True,  # noqa: E712
                Provider.deleted_at.is_(None),
            )
        )
        providers = list(res.scalars().all())
    # v3.0.49: skip probes on providers whose circuit breaker is open
    # (billing-quota-exhausted, auth-revoked, or repeated transient
    # failures). When the breaker closes, probes resume automatically.
    from app.routing.circuit_breaker import is_available
    # v3.0.56: skip probes on PER-CALL provider types by default.
    # Synthetic probes on Cohere / OpenAI / Vertex / Google etc. burn
    # real $ every 5 min × 24h × 4 nodes = ~1152 probes/day per
    # provider × ~$0.001/call → ~$120/year/provider in pure synthetic
    # noise. Subscription-tier providers (claude-oauth, codex-oauth,
    # anthropic-oauth) cost $0 per probe so we keep probing them.
    # Operator can re-enable per-call probes globally via
    # settings.keepalive_probe_per_call_providers=True if needed.
    probe_per_call = getattr(
        settings, "keepalive_probe_per_call_providers", False
    )
    # v3.2.10: grok-web added — operator's grok.com subscription is
    # cost=$0 per probe (same as other OAuth subscriptions), so worth
    # the every-5-min health check. Without this, the bridge container
    # could lose its session and we wouldn't notice until organic
    # traffic 401s. Probes now fire grok-web → bridge → grok.com,
    # exercising the full pipeline including Cloudflare cookie freshness.
    SUBSCRIPTION_TYPES = {"claude-oauth", "ChatGPT-oauth-plan", "anthropic-oauth", "grok-web"}
    for p in providers:
        if not probe_per_call and p.provider_type not in SUBSCRIPTION_TYPES:
            logger.debug(
                "keepalive.skipped_per_call provider=%s type=%s",
                p.id, p.provider_type,
            )
            continue
        # v5.9.2 — check auto_skip_until BEFORE is_available. v5.8.7
        # added the auto_skip_until gate but placed it AFTER the
        # is_available CB check. The CB's hold_down expires every 120s,
        # and the keepalive sweep aligns close enough to that window
        # that probes slip through, fail, record_failure resets
        # hold_down → 120s, and the cycle repeats indefinitely. Moving
        # the auto_skip_until check first means "operator must re-auth"
        # is the strongest signal — it strictly dominates CB hysteresis
        # and silences the cycle.
        auto_skip_until = getattr(p, "auto_skip_until", None)
        if auto_skip_until is not None:
            try:
                from datetime import datetime as _dt, timezone as _tz
                if hasattr(auto_skip_until, "tzinfo"):
                    askdt = auto_skip_until
                else:
                    askdt = _dt.fromisoformat(str(auto_skip_until).replace("Z", "+00:00"))
                if askdt.tzinfo is None:
                    askdt = askdt.replace(tzinfo=_tz.utc)
                if askdt > _dt.now(_tz.utc):
                    logger.debug(
                        "keepalive.skipped_auto_skip provider=%s until=%s",
                        p.id, askdt.isoformat(),
                    )
                    continue
            except Exception:
                pass
        if not await is_available(p.id):
            logger.debug("keepalive.skipped_breaker_open provider=%s", p.id)
            continue
        # v5.17.1 chronic-CB gate MOVED to ``_probe_one`` in v5.19.1 so
        # both the sweep path AND the ``circuit_breaker._auto_probe`` path
        # honor it. The auto-probe was slipping through the sweep-side
        # gate and re-cycling Grok every ~65 min (CB hold-down window).
        # v3.3.3: skip providers in rate-limit back-off window.
        if _backoff_skip(p.id):
            logger.debug(
                "keepalive.skipped_rate_limit_backoff provider=%s remaining=%.0fs",
                p.id, max(0.0, _probe_backoff_until.get(p.id, 0.0) - time.time()),
            )
            continue
        try:
            await _probe_one(p)
            count += 1
        except Exception as e:
            logger.info("keepalive.probe_failed provider=%s err=%s", p.id, e)
    return count


async def _probe_loop() -> None:
    """Periodic loop. Fires the first sweep ~30s after startup (so the
    rest of the boot finishes), then on the configured interval."""
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="keepalive")
    await asyncio.sleep(30)  # let providers register, db settle
    while True:
        interval = _probe_interval_sec()
        register_expected_interval("keepalive", interval or 60)
        if interval == 0:
            await hb.tick(status="disabled", note="probe_interval_sec=0")
            await asyncio.sleep(60)  # check setting again in a minute
            continue
        try:
            n = await _probe_all_once()
            if n:
                logger.info("keepalive.swept count=%d", n)
            await hb.tick(status="ok", note=f"swept count={n}")
        except Exception as e:
            logger.warning("keepalive.sweep_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(interval)


_TASK: Optional[asyncio.Task] = None


def start() -> None:
    """Spawn the periodic probe loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_probe_loop(), name="keepalive-probe-loop")
