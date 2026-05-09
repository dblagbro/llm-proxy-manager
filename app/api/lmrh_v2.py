"""
LMRH v2 endpoints — bidirectional metrics feedback channel.

Operator-approved 2026-05-09; design doc lives in
``project_lmrhv2_design.md`` (memory). Phase 1 surface (this module):

  GET /.well-known/lmrh-config   — protocol metadata, polling guidance
  GET /lmrh/providers            — live snapshot of providers + metrics
  GET /lmrh/providers/{id}       — single-provider deep view
  GET /lmrh/health               — fleet health summary

Auth: same API key as /v1/messages. Per-key scope filter applied at
render time (operator decision #1 — only providers this key can route
to). Anonymous access only on `/.well-known/lmrh-config` per RFC 8615.

Feature flag: ``lmrh_v2_enabled`` setting. When False (default), all
endpoints return ``404 Not Found`` so v1.x callers don't see them
until operator flips per-node.

Rate limiting: per-key, default 4/min for /lmrh/providers (since the
underlying snapshot only refreshes every 30s, faster polling returns
duplicates). Override via new ``ApiKey.lmrh_polling_rpm`` column.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keys import ApiKeyRecord, resolve_api_key_dep
from app.config import settings
from app.models.database import get_db
from app.models.db import ApiKey
from app.routing.lmrh import snapshot as snap_mod

router = APIRouter(tags=["lmrh-v2"])

# ── Feature flag gate ─────────────────────────────────────────────────


def _v2_enabled() -> bool:
    """Read the lmrh_v2_enabled runtime flag. Default False until
    operator flips per-node. ``apply()`` in config_runtime patches
    settings.lmrh_v2_enabled at boot from the SystemSetting row."""
    return bool(getattr(settings, "lmrh_v2_enabled", False))


def _ensure_enabled() -> None:
    if not _v2_enabled():
        # 404 (not 503) so v1.x callers can't probe whether v2 is
        # installed-but-disabled vs not-installed — endpoint just
        # doesn't exist as far as they know.
        raise HTTPException(404, "Not Found")


# ── Rate limiting ────────────────────────────────────────────────────


# In-process per-key rate-limit state. (key_id, endpoint) → list[float]
# of recent request timestamps. Pruned + checked on each call.
_rate_state: dict[tuple[str, str], list[float]] = defaultdict(list)
_rate_lock = asyncio.Lock()

# Defaults from operator decision #5
DEFAULT_PROVIDERS_RPM = 4
DEFAULT_QUOTES_RPM = 60


async def _check_rate_limit(
    db: AsyncSession,
    key: ApiKeyRecord,
    endpoint: str,  # "providers" | "quotes"
) -> None:
    """Per-key sliding-window rate limit. ``ApiKey.lmrh_polling_rpm``
    overrides the default for the providers endpoint;
    ``ApiKey.lmrh_quotes_rpm`` for quotes. Null on either column =
    use the default."""
    # Look up overrides once per call. ApiKeyRecord is the lightweight
    # in-memory shape; the override columns live on the full ApiKey row.
    db_key = await db.get(ApiKey, key.id)
    override = None
    if db_key:
        if endpoint == "providers":
            override = getattr(db_key, "lmrh_polling_rpm", None)
        elif endpoint == "quotes":
            override = getattr(db_key, "lmrh_quotes_rpm", None)
    rpm = override or (
        DEFAULT_PROVIDERS_RPM if endpoint == "providers" else DEFAULT_QUOTES_RPM
    )
    window_sec = 60.0
    now = time.time()
    state_key = (key.id, endpoint)
    async with _rate_lock:
        timestamps = _rate_state[state_key]
        # Drop timestamps older than the window
        cutoff = now - window_sec
        timestamps[:] = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= rpm:
            # Compute Retry-After from the oldest timestamp in window
            retry_after = max(1, int(window_sec - (now - timestamps[0])))
            raise HTTPException(
                429,
                f"LMRH polling rate limit exceeded ({rpm}/min). "
                f"Retry in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        timestamps.append(now)


# ── Snapshot rendering ────────────────────────────────────────────────


def _render_provider(p: snap_mod._ProviderSnap) -> dict:
    """Convert a _ProviderSnap to the wire-format dict per design §4.3.

    Drops fields that should NEVER ride the wire (e.g.
    ``owned_by_key_id`` is internal-only; the scope filter ran before
    this; we don't tell callers WHO else can route to a provider).
    """
    out = {
        "id": p.id,
        "name": p.name,
        "type": p.type,
        "priority": p.priority,
        "cost_class": p.cost_class,
        "circuit": p.circuit,
        "regions": p.regions,
        "models": [
            {
                "model_id": m.model_id,
                "kind": m.kind,
                "context_length": m.context_length,
                "native_tools": m.native_tools,
                "native_reasoning": m.native_reasoning,
                # v3.5.0 (LMRHv2.1) — model identity surfaced as
                # siblings of model_id. ``aliases`` lists alternate
                # spellings the proxy will accept (caller can send
                # any of them and route to this same model row).
                # ``family`` is the upstream physical model identity;
                # ``variant`` is the route flavour. When two model
                # entries share the same family but different
                # variants, they are multi-route access to the SAME
                # underlying model — caller can pick by cost / latency
                # / reliability rather than picking blindly. See spec
                # §"Model identity model" for the full taxonomy.
                "aliases": list(m.aliases or []),
                "family": m.family,
                "variant": m.variant,
                "metrics": {
                    "cost_per_1m_input_usd": m.cost_per_1m_input_usd,
                    "cost_per_1m_output_usd": m.cost_per_1m_output_usd,
                    "rated_quota_per_1m_input_usd": m.rated_quota_per_1m_input_usd,
                    "latency_p50_ms": m.latency_p50_ms,
                    "latency_p95_ms": m.latency_p95_ms,
                    "ttft_p50_ms": m.ttft_p50_ms,
                    "ttft_p95_ms": m.ttft_p95_ms,
                    # v3.3.3+: success_rate / samples = USER traffic only
                    "success_rate": m.success_rate,
                    "samples": m.samples,
                    # v3.3.4+: separate channel for synthetic probe outcomes
                    # so callers can read connectivity health alongside
                    # user-traffic reliability. None when no probes ran in
                    # the window. See docs/lmrh-2.0-bidirectional.md
                    # "Probe vs user-traffic metrics" for semantics.
                    "probe_success_rate": m.probe_success_rate,
                    "probe_samples": m.probe_samples,
                },
            }
            for m in p.models
        ],
    }
    if p.subscription_quota is not None:
        out["subscription_quota"] = {
            "session_used_pct": p.subscription_quota.session_used_pct,
            "weekly_used_pct": p.subscription_quota.weekly_used_pct,
            "session_resets_at": p.subscription_quota.session_resets_at,
            "weekly_resets_at": p.subscription_quota.weekly_resets_at,
        }
    return out


# ── /.well-known/lmrh-config ──────────────────────────────────────────


@router.get("/.well-known/lmrh-config")
async def well_known_config() -> dict:
    """Public — server metadata describing the LMRH protocol surface
    available on this proxy. RFC 8615 well-known URI pattern.

    Doesn't gate on lmrh_v2_enabled because clients use this to
    discover whether v2 is available — returning 404 here would be
    indistinguishable from "the proxy doesn't support LMRH at all".
    Instead, we emit ``versions: ["1.x"]`` only when v2 is off so
    the client knows what they can use.
    """
    versions = ["1.2"]
    endpoints = {
        "registry": "/lmrh/registry",
    }
    polling = {}
    cache = {"registry_max_age_sec": 3600}
    if _v2_enabled():
        # v3.5.0 advertises both 2.0 and 2.1 — clients negotiating
        # against ``supported_versions`` pick the highest they can
        # parse. 2.1 is additive (aliases/family/variant on each
        # model entry); 2.0 clients get those fields harmlessly
        # ignored as unknown JSON keys.
        versions.append("2.0")
        versions.append("2.1")
        endpoints.update({
            "providers": "/lmrh/providers",
            "providers_one": "/lmrh/providers/{provider_id}",
            "quotes": "/lmrh/quotes",
            "health": "/lmrh/health",
            # v3.3.2: pointer to the public LMRHv2 spec served by the
            # proxy itself. Lets clients self-document.
            "spec": "/lmrh/v2.md",
            # v3.4.0: Server-Sent Events stream — same payload as
            # /lmrh/providers but pushed when the underlying snapshot
            # ETag changes, eliminating the polling-then-304 dance for
            # clients that prefer push.
            "stream": "/lmrh/stream",
        })
        polling = {
            "providers_min_interval_sec": 15,
            "providers_recommended_interval_sec": 60,
            "providers_max_rate_per_minute": DEFAULT_PROVIDERS_RPM,
            "quotes_max_rate_per_minute": DEFAULT_QUOTES_RPM,
            # v3.4.0: clients using /lmrh/stream don't need to poll —
            # included here for completeness so config consumers can
            # see the recommended cadence at a glance.
            "stream_recommended": True,
        }
        cache.update({
            "providers_max_age_sec": 30,
            "health_max_age_sec": 30,
        })
    return {
        "version": "2.0" if _v2_enabled() else "1.2",
        "supported_versions": versions,
        "endpoints": endpoints,
        "polling": polling,
        "cache": cache,
        "supported_dims": [
            "task", "cost", "latency", "region",
            "cache", "provider-hint", "exclude",
            "cost-class", "tools", "vision",
        ],
    }


# ── /lmrh/providers + /lmrh/providers/{id} ────────────────────────────


@router.get("/lmrh/providers")
async def get_providers(
    request: Request,
    response: Response,
    type: Optional[str] = None,
    capability: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(resolve_api_key_dep()),
) -> Response:
    """Snapshot of providers visible to this caller, with live metrics.

    Filters:
      - ``?type=`` — restrict to a single provider_type
      - ``?capability=`` — restrict to providers offering at least one
        model with the given kind (chat / embedding / image / audio)

    Conditional GET via ``If-None-Match`` honored. Cache-Control 30s.
    """
    _ensure_enabled()
    await _check_rate_limit(db, key, "providers")

    cur = snap_mod.get_current()
    if cur is None:
        # Refresh loop hasn't completed first build yet (or it was
        # restarted moments ago). Force-build a snapshot inline so the
        # caller still gets data; this only happens at boot.
        cur = await snap_mod.rebuild_now(db)

    # Conditional GET — return 304 if client already has this etag
    inm = request.headers.get("if-none-match")
    if inm and inm == cur.etag:
        response.status_code = 304
        response.headers["ETag"] = cur.etag
        response.headers["Cache-Control"] = "max-age=30"
        return response

    # Scope filter (operator decision #1)
    visible = cur.for_caller(key.id)

    # Optional filters from query params
    if type:
        visible = [p for p in visible if p.type == type]
    if capability:
        visible = [
            p for p in visible
            if any(m.kind == capability for m in p.models)
        ]

    body = {
        "version": "2.1",
        "as_of": cur.as_of.isoformat(),
        "window_sec": cur.window_sec,
        "providers": [_render_provider(p) for p in visible],
    }
    import json as _json
    payload = _json.dumps(body, default=str).encode()
    response.headers["ETag"] = cur.etag
    response.headers["Cache-Control"] = "max-age=30"
    response.headers["Content-Type"] = "application/json"
    response.body = payload
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "ETag": cur.etag,
            "Cache-Control": "max-age=30",
        },
    )


@router.get("/lmrh/providers/{provider_id}")
async def get_provider_one(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(resolve_api_key_dep()),
) -> dict:
    """Single-provider deep view. 404 if the provider doesn't exist or
    the caller's key isn't allowed to route to it (don't leak existence
    of operator-private providers)."""
    _ensure_enabled()
    await _check_rate_limit(db, key, "providers")

    cur = snap_mod.get_current()
    if cur is None:
        cur = await snap_mod.rebuild_now(db)
    visible = cur.for_caller(key.id)
    for p in visible:
        if p.id == provider_id:
            return {
                "version": "2.0",
                "as_of": cur.as_of.isoformat(),
                "provider": _render_provider(p),
            }
    raise HTTPException(404, "provider not found")


# ── /lmrh/quotes ───────────────────────────────────────────────────────


@router.get("/lmrh/quotes")
async def get_quotes(
    request: Request,
    response: Response,
    model: str,
    hint: Optional[str] = None,
    has_tools: bool = False,
    has_images: bool = False,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(resolve_api_key_dep()),
) -> dict:
    """Pre-flight an inference request without dispatching it.

    Returns the proxy's ranked candidate list for ``model`` + optional
    ``hint`` — same scoring path that ``/v1/messages`` uses, just stops
    before winner-pick + dispatch. Lets sophisticated callers see what
    WOULD happen for a given hint, so they can adjust before sending
    real traffic.

    Per operator decision #5: separate rate-limit budget from
    /lmrh/providers (default 60/min vs providers' 4/min) since
    /quotes is per-call and the response shape varies, so caching is
    less useful than for the bulk providers endpoint.

    Joins predicted cost/latency from the snapshot at render time so
    each candidate carries the same metric set as /lmrh/providers
    (samples, latency_p50_ms, etc.).
    """
    _ensure_enabled()
    await _check_rate_limit(db, key, "quotes")

    if not model:
        raise HTTPException(400, "model query param is required")

    # Parse hint (LMRH 1.x) if provided
    parsed_hint = None
    if hint:
        try:
            from app.routing.lmrh.parse import parse_hint as _parse_hint
            parsed_hint = _parse_hint(hint)
        except Exception as e:
            raise HTTPException(400, f"invalid hint: {e}")

    # Set the tenant ContextVar so select_provider's ownership filter
    # picks up THIS caller's key. Without this the dry-run would see
    # providers any random caller can route to, including operator-
    # private ones owned by other keys — which would then NOT be in
    # the snapshot scope filter and confuse the predicted-metrics join.
    from app.routing import tenant
    tok = tenant.current_api_key_id.set(key.id)
    try:
        from app.routing.router import select_provider
        try:
            ranked = await select_provider(
                db,
                hint=parsed_hint,
                has_tools=has_tools,
                has_images=has_images,
                key_type=key.key_type,
                model_override=model,
                api_key_id=key.id,
                dry_run=True,
            )
        except RuntimeError as e:
            # No candidates — the same 503 the dispatch path would raise.
            raise HTTPException(
                503,
                f"No providers satisfy these constraints: {e}",
            )
    finally:
        tenant.current_api_key_id.reset(tok)

    # Cross-reference with snapshot for predicted metrics
    cur = snap_mod.get_current()
    if cur is None:
        cur = await snap_mod.rebuild_now(db)
    visible = {p.id: p for p in cur.for_caller(key.id)}

    candidates = []
    for rank_pos, item in enumerate(ranked, start=1):
        provider = item["provider"]
        profile = item["profile"]
        score = item["score"]
        unmet = item["unmet"]

        # Pull predicted metrics from the snapshot (already joined)
        snap_p = visible.get(provider.id)
        predicted_latency_p50 = None
        predicted_latency_p95 = None
        predicted_cost = None
        predicted_quota = None
        success_rate = None
        samples = 0
        if snap_p:
            # Pick the model row in the snapshot that matches the
            # caller's requested model_override; fall back to the
            # provider's first model entry if no exact match.
            chosen_m = None
            for m in snap_p.models:
                if m.model_id == model or m.model_id == profile.model_id:
                    chosen_m = m
                    break
            if chosen_m is None and snap_p.models:
                chosen_m = snap_p.models[0]
            if chosen_m:
                predicted_latency_p50 = chosen_m.latency_p50_ms
                predicted_latency_p95 = chosen_m.latency_p95_ms
                predicted_cost = chosen_m.cost_per_1m_input_usd
                predicted_quota = chosen_m.rated_quota_per_1m_input_usd
                success_rate = chosen_m.success_rate
                samples = chosen_m.samples

        candidates.append({
            "rank": rank_pos,
            "provider_id": provider.id,
            "provider_name": provider.name,
            "model_id": profile.model_id,
            "score": score,
            "unmet_hints": unmet,
            "cost_class": (
                "subscription" if (snap_p and snap_p.cost_class == "subscription")
                else "per_call"
            ),
            "circuit": (snap_p.circuit if snap_p else "closed"),
            "predicted_latency_p50_ms": predicted_latency_p50,
            "predicted_latency_p95_ms": predicted_latency_p95,
            "predicted_cost_per_1m_input_usd": predicted_cost,
            "predicted_quota_per_1m_input_usd": predicted_quota,
            "success_rate": success_rate,
            "samples": samples,
        })

    return {
        "version": "2.0",
        "as_of": cur.as_of.isoformat() if cur else None,
        "requested": {
            "model": model,
            "hint": hint,
            "has_tools": has_tools,
            "has_images": has_images,
        },
        "candidates": candidates,
    }


# ── /lmrh/health ──────────────────────────────────────────────────────


# ── /lmrh/stream — Server-Sent Events push (v3.4.0) ───────────────────


@router.get("/lmrh/stream")
async def stream_snapshot(
    request: Request,
    heartbeat_sec: int = Query(
        25, ge=10, le=120,
        description="Seconds between SSE heartbeat (`: ping`) frames. "
                    "Keeps proxies / load-balancers from idle-timing the "
                    "long-lived connection. Default 25s.",
    ),
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(resolve_api_key_dep()),
):
    """Server-Sent Events stream of LMRHv2 snapshot updates (v3.4.0+).

    Pushes the full snapshot when the underlying ETag changes. Clients
    that prefer push semantics (vs polling /lmrh/providers every 30s)
    consume this and avoid the per-poll round-trip + 304 dance.

    Flow:
      - On connect: ``event: snapshot`` with current snapshot body
      - On ETag change: ``event: snapshot`` with new body (max ~30s
        latency since the underlying refresh loop runs every 30s)
      - Every ``heartbeat_sec``: ``: ping\\n\\n`` to defeat idle timeouts

    Closes when the client disconnects (FastAPI detects via
    ``request.is_disconnected()``).

    Auth: same per-key scope as /lmrh/providers. Per-key ``providers``
    rate limit applies on connect (the long-lived connection itself
    is not rate-limited; one connection per key is the design).
    """
    _ensure_enabled()
    await _check_rate_limit(db, key, "providers")
    key_id = key.id

    async def event_gen():
        import json as _json
        last_etag: Optional[str] = None
        last_heartbeat = time.time()
        # First frame: emit current snapshot synchronously so the client
        # gets data within the first round-trip rather than waiting for
        # the next refresh tick.
        cur = snap_mod.get_current() or await snap_mod.rebuild_now()
        visible = cur.for_caller(key_id)
        body = {
            "version": "2.0",
            "as_of": cur.as_of.isoformat(),
            "window_sec": cur.window_sec,
            "providers": [_render_provider(p) for p in visible],
        }
        yield (
            f"event: snapshot\n"
            f"id: {cur.etag.strip(chr(34))}\n"
            f"data: {_json.dumps(body, default=str)}\n\n"
        ).encode()
        last_etag = cur.etag

        # Stream loop: poll the in-memory snapshot module every 1s, push
        # when etag changes; emit heartbeat every heartbeat_sec.
        while True:
            if await request.is_disconnected():
                return
            await asyncio.sleep(1.0)
            cur = snap_mod.get_current()
            if cur is None:
                continue
            now = time.time()
            if cur.etag != last_etag:
                visible = cur.for_caller(key_id)
                body = {
                    "version": "2.0",
                    "as_of": cur.as_of.isoformat(),
                    "window_sec": cur.window_sec,
                    "providers": [_render_provider(p) for p in visible],
                }
                yield (
                    f"event: snapshot\n"
                    f"id: {cur.etag.strip(chr(34))}\n"
                    f"data: {_json.dumps(body, default=str)}\n\n"
                ).encode()
                last_etag = cur.etag
                last_heartbeat = now
            elif now - last_heartbeat >= heartbeat_sec:
                yield b": ping\n\n"
                last_heartbeat = now

    # No buffering: each chunk should flush. nginx is configured to
    # not buffer text/event-stream by default but we set the header
    # explicitly per RFC.
    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",  # nginx-specific: defeat proxy buffering
            "Connection": "keep-alive",
        },
    )


@router.get("/lmrh/health")
async def get_health(
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(resolve_api_key_dep()),
) -> dict:
    """Aggregate fleet health for this caller's visible providers.

    Returns:
      - total_providers: int
      - circuit_open_count: int  (any breaker open)
      - degraded_count: int  (success_rate < 0.95 with samples ≥ 10)
      - last_snapshot_age_sec: int  (so callers detect stale data)
    """
    _ensure_enabled()
    await _check_rate_limit(db, key, "providers")  # share the providers rpm

    cur = snap_mod.get_current()
    if cur is None:
        cur = await snap_mod.rebuild_now(db)
    visible = cur.for_caller(key.id)
    open_count = sum(1 for p in visible if p.circuit == "open")
    degraded = 0
    for p in visible:
        for m in p.models:
            if m.success_rate is not None and m.samples >= 10 and m.success_rate < 0.95:
                degraded += 1
                break
    age = (datetime.now(timezone.utc) - cur.as_of).total_seconds()
    return {
        "version": "2.0",
        "as_of": cur.as_of.isoformat(),
        "last_snapshot_age_sec": int(age),
        "total_providers": len(visible),
        "circuit_open_count": open_count,
        "degraded_count": degraded,
    }
