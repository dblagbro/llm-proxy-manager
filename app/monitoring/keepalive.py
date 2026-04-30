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
    keep-alive is best-effort, doesn't block routing."""
    model = provider.default_model or "gpt-4o-mini"
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
    elif provider.provider_type == "codex-oauth":
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
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SEC) as _c:
                async with _c.stream(
                    "POST", CODEX_RESPONSES_URL, headers=headers, json=codex_body,
                ) as _r:
                    if _r.status_code >= 400:
                        body = await _r.aread()
                        err_str = f"{_r.status_code}: {body[:300].decode(errors='replace')}"
                    else:
                        # Drain enough events to confirm response.completed.
                        async for line in _r.aiter_lines():
                            if line.startswith("data:") and "response.completed" in line:
                                success = True
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
    for p in providers:
        # v3.0.2 follow-up: claude-oauth providers ARE probed (via the
        # OAuth dispatch path in _probe_one) so the activity log shows
        # liveness for them too. Probes fire every interval regardless
        # of organic traffic — the cost is small (8 max_tokens × N
        # providers per interval) and the operator wants the at-a-glance
        # liveness signal in the activity log either way.
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
