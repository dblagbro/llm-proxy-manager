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


# v4.3.2 — providers that require a docker-network sidecar (today: grok-web
# via grok-bridge) are part of the cluster-synced provider config, but the
# sidecar itself is per-node infrastructure. On a node where the sidecar is
# absent, every probe to the provider fails with a connection error — noisy
# and pointless. The interim fix here is a per-(provider_id) "no local
# sidecar" flag the prober sets when it detects the sidecar is unreachable;
# it suppresses further probes (logged once, not per-probe) until the
# sidecar comes back. The v4.4 per-node-auth-state arc supersedes this with
# a synced cluster-wide view + a guided cross-node auth flow.
_no_local_sidecar: set[str] = set()


def is_no_local_sidecar(provider_id: str) -> bool:
    """True if the prober has detected that this provider's required local
    sidecar is unreachable on this node. Routing / dispatch callers may
    consult this to skip a provider that cannot be served locally."""
    return provider_id in _no_local_sidecar


async def _local_sidecar_reachable(url: str) -> bool:
    """Quick reachability check on a sidecar URL. Returns False on a
    connection error / DNS failure / connect timeout (i.e. the sidecar
    isn't deployed on this node); True on any HTTP response, even an error
    one — a response means the sidecar is there (just maybe not happy)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(
                connect=2.0, read=2.0, write=2.0, pool=2.0)) as c:
            await c.get(url)
        return True
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False
    except Exception:
        # Any non-connection error means we reached *something* — treat as
        # reachable so we don't suppress probes for a present-but-odd bridge.
        return True


async def _probe_one(provider: Provider) -> None:
    """Send one synthetic call to a provider. All errors swallowed —
    keep-alive is best-effort, doesn't block routing."""
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
        #
        # v4.3.2: bridge-mode providers require a local grok-bridge on the
        # docker network. If the bridge isn't deployed on this node, every
        # probe would fail with a connection error and trip the CB — noisy
        # and pointless. Skip the probe silently in that case; it resumes
        # automatically when the bridge appears.
        _cfg = provider.extra_config or {}
        _bridge_url = (_cfg.get("bridge_url") or "").rstrip("/")
        if _bridge_url and not await _local_sidecar_reachable(_bridge_url):
            if provider.id not in _no_local_sidecar:
                _no_local_sidecar.add(provider.id)
                logger.info(
                    "keepalive: skipping %s (id=%s) — no local grok-bridge "
                    "at %s; the provider is configured cluster-wide but the "
                    "required sidecar isn't deployed on this node.",
                    provider.name, provider.id, _bridge_url,
                )
            return
        if provider.id in _no_local_sidecar:
            _no_local_sidecar.discard(provider.id)
            logger.info(
                "keepalive: grok-bridge reachable for %s — resuming probes",
                provider.name,
            )
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
        if not await is_available(p.id):
            logger.debug("keepalive.skipped_breaker_open provider=%s", p.id)
            continue
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
    await asyncio.sleep(30)  # let providers register, db settle
    while True:
        interval = _probe_interval_sec()
        if interval == 0:
            await asyncio.sleep(60)  # check setting again in a minute
            continue
        try:
            n = await _probe_all_once()
            if n:
                logger.info("keepalive.swept count=%d", n)
        except Exception as e:
            logger.warning("keepalive.sweep_failed err=%s", e)
        await asyncio.sleep(interval)


_TASK: Optional[asyncio.Task] = None


def start() -> None:
    """Spawn the periodic probe loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_probe_loop(), name="keepalive-probe-loop")
