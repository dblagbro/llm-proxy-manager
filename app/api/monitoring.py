"""Monitoring, metrics, and activity log endpoints."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.models.database import get_db
from app.models.db import ActivityLog
from app.auth.admin import require_admin, AdminUser
from app.monitoring.activity import get_recent, subscribe, unsubscribe
from app.monitoring.metrics import get_provider_history, get_all_provider_summary
from app.monitoring.status import get_status_summary
from app.routing.circuit_breaker import get_all_states
from app.utils.timefmt import utc_iso

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/activity")
async def activity_log(
    limit: int = Query(100, le=1000),
    severity: Optional[str] = None,
    provider_id: Optional[str] = None,
    api_key_id: Optional[str] = Query(None, description="v3.0.35: filter to a single API key's events"),
    event_type: Optional[str] = Query(None, description="v3.0.35: filter to a single event_type (e.g. llm_request)"),
    error_class: Optional[str] = Query(None, description="v3.0.78: filter to a single error_class bucket (auth, billing, rate_limit, timeout, network, upstream_5xx, bad_request, unknown)"),
    since: Optional[str] = Query(None, description="v3.0.35: ISO 8601 timestamp lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="v3.0.35: ISO 8601 timestamp upper bound (exclusive)"),
    sort: str = Query("desc", description="v3.0.35: 'desc' (default, newest first) or 'asc' (oldest first)"),
    before_id: Optional[int] = Query(None, description="Return events with id < this; cursor for paging back"),
    search: Optional[str] = Query(None, description="Substring match across message, provider_id, and metadata"),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v2.8.5 paginated + searchable; v3.0.35 adds per-key + per-event-type
    filters, ISO timestamp range, and asc/desc sort.

    ``before_id`` is the cursor for desc paging (pass the smallest id from
    the prior page). For asc, ``since`` is the natural cursor.
    """
    from sqlalchemy import cast, String
    from datetime import datetime as _dt

    if sort.lower() == "asc":
        order_clause = ActivityLog.created_at.asc()
    else:
        order_clause = desc(ActivityLog.created_at)

    query = select(ActivityLog).order_by(order_clause).limit(limit)

    if before_id is not None:
        query = query.where(ActivityLog.id < before_id)

    if provider_id:
        query = query.where(ActivityLog.provider_id == provider_id)

    if api_key_id:
        # v3.0.35: per-key filter — column already exists, just expose it.
        # Operator + DevinGPT both asked for this on 2026-05-01.
        query = query.where(ActivityLog.api_key_id == api_key_id)

    if event_type:
        query = query.where(ActivityLog.event_type == event_type)

    if error_class:
        # v3.0.78: filter by event_meta.error_class (the v3.0.75 taxonomy
        # bucket). Comma-separated list supported for OR semantics, e.g.
        # ?error_class=rate_limit,timeout. SQLite json_extract works on
        # both string and dict-typed JSON columns.
        from sqlalchemy import func
        ec_list = [e.strip() for e in error_class.split(",") if e.strip()]
        if len(ec_list) == 1:
            query = query.where(
                func.json_extract(ActivityLog.event_meta, "$.error_class") == ec_list[0]
            )
        elif ec_list:
            query = query.where(
                func.json_extract(ActivityLog.event_meta, "$.error_class").in_(ec_list)
            )

    def _parse_iso(s: str):
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None

    if since:
        since_dt = _parse_iso(since)
        if since_dt is not None:
            query = query.where(ActivityLog.created_at >= since_dt)

    if until:
        until_dt = _parse_iso(until)
        if until_dt is not None:
            query = query.where(ActivityLog.created_at < until_dt)

    if severity:
        sev_list = [s.strip() for s in severity.split(",") if s.strip()]
        if len(sev_list) == 1:
            query = query.where(ActivityLog.severity == sev_list[0])
        elif sev_list:
            query = query.where(ActivityLog.severity.in_(sev_list))

    if search:
        # SQLite has no native FTS on JSON columns; do a case-insensitive
        # substring match against (message, provider_id, JSON-stringified
        # event_meta). Cheap for the common <100k row case; if tables get
        # big, add a dedicated FTS5 virtual table later.
        s = f"%{search}%"
        query = query.where(
            (ActivityLog.message.ilike(s))
            | (ActivityLog.provider_id.ilike(s))
            | (cast(ActivityLog.event_meta, String).ilike(s))
        )

    result = await db.execute(query)
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "severity": r.severity,
            "message": r.message,
            "provider_id": r.provider_id,
            "api_key_id": r.api_key_id,  # v3.0.35: surface column for client-side correlation
            "timestamp": utc_iso(r.created_at),
            "metadata": r.event_meta,
        }
        for r in rows
    ]


@router.get("/activity/count")
async def activity_count(
    severity: Optional[str] = None,
    provider_id: Optional[str] = None,
    error_class: Optional[str] = Query(None, description="v3.0.78: filter to a single error_class bucket (or comma-separated for OR)"),
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v2.8.5: total matching event count for the current filter — lets
    the UI show "showing 200 of N" so operators know how much they can
    page back through. v3.0.78 adds error_class for parity with /activity."""
    from sqlalchemy import func, cast, String
    query = select(func.count(ActivityLog.id))
    if provider_id:
        query = query.where(ActivityLog.provider_id == provider_id)
    if severity:
        sev_list = [s.strip() for s in severity.split(",") if s.strip()]
        if len(sev_list) == 1:
            query = query.where(ActivityLog.severity == sev_list[0])
        elif sev_list:
            query = query.where(ActivityLog.severity.in_(sev_list))
    if error_class:
        ec_list = [e.strip() for e in error_class.split(",") if e.strip()]
        if len(ec_list) == 1:
            query = query.where(
                func.json_extract(ActivityLog.event_meta, "$.error_class") == ec_list[0]
            )
        elif ec_list:
            query = query.where(
                func.json_extract(ActivityLog.event_meta, "$.error_class").in_(ec_list)
            )
    if search:
        s = f"%{search}%"
        query = query.where(
            (ActivityLog.message.ilike(s))
            | (ActivityLog.provider_id.ilike(s))
            | (cast(ActivityLog.event_meta, String).ilike(s))
        )
    total = (await db.execute(query)).scalar() or 0
    return {"total": int(total)}


@router.get("/activity/stream")
async def activity_stream(_: AdminUser = Depends(require_admin)):
    """SSE stream of live activity events for the dashboard."""
    q = subscribe()

    async def _gen():
        # Send recent history first
        for event in get_recent(50):
            import json
            yield f"data: {json.dumps(event)}\n\n"
        # Then live events
        try:
            while True:
                import asyncio
                import json
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            unsubscribe(q)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/metrics")
async def metrics_summary(
    hours: int = Query(24, le=720),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    summary = await get_all_provider_summary(db, hours=hours)
    circuit_states = get_all_states()
    return {"hours": hours, "providers": summary, "circuit_breakers": circuit_states}


@router.get("/metrics/{provider_id}")
async def provider_metrics(
    provider_id: str,
    hours: int = Query(24, le=720),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    history = await get_provider_history(db, provider_id, hours=hours)
    return {"provider_id": provider_id, "hours": hours, "buckets": history}


@router.get("/status-pages")
async def external_status(_: AdminUser = Depends(require_admin)):
    return await get_status_summary()


# v3.0.72 — Cache-effectiveness rollup. Surfaces the cache_read /
# cache_creation token counts that v3.0.71 started writing to
# event_meta. Fleet currently reads ~650K cache_read tokens/hr on www01
# alone (43.9% hit rate verified 2026-05-06); operator dashboards need
# a stable endpoint to chart against rather than ad-hoc Python in a
# docker exec.
@router.get("/cache-stats")
async def cache_stats(
    window_minutes: int = Query(60, ge=1, le=1440, description="rolling window size"),
    group_by: str = Query("provider", description="'provider' (default), 'api_key', or 'none'"),
    bucket_minutes: int = Query(
        0, ge=0, le=1440,
        description="v3.0.80: when >0, return a ``time_series`` array bucketing the window into N-minute slices. 0 = aggregate-only response (default).",
    ),
    rate_per_million: float = Query(
        3.0,
        description="Per-million-token rate for the savings estimate "
        "(default 3.0 ≈ Claude Sonnet 4.6 input). Caller picks model price.",
    ),
    cache_discount_pct: float = Query(
        0.9, ge=0.0, le=1.0,
        description="Cache-read discount fraction (default 0.9 = Anthropic's 90%).",
    ),
    _: AdminUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate cache effectiveness from event_meta over a rolling window.

    Source: ``ActivityLog.event_meta`` keys ``cache_read_input_tokens``,
    ``cache_creation_input_tokens``, ``in_tok`` (NEW input tokens),
    populated since v3.0.71.

    The savings estimate is a rough order-of-magnitude — it treats every
    cache_read token as if it would have cost ``rate_per_million`` at full
    rate but actually cost ``rate * (1 - discount)``. Reality: cache reads
    are billed at 10% on Anthropic per the prompt-cache pricing, so the
    saved-portion is 90% of what those tokens would have otherwise cost.
    Operator can re-run with their own rate for per-model accuracy.
    """
    import json
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select, desc

    from app.models.db import Provider, ApiKey

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    rows = (await db.execute(
        select(
            ActivityLog.event_meta, ActivityLog.provider_id, ActivityLog.api_key_id,
            ActivityLog.created_at,
        )
        .where(ActivityLog.created_at >= cutoff)
        .where(ActivityLog.event_type == "llm_request")
        .where(ActivityLog.severity == "info")
        # Cap at a sane upper bound — even at 1k events/min for 24h that's
        # 1.4M rows; the in-Python aggregation tops out around 100k before
        # latency becomes user-visible. Operator picks a tighter window
        # for high-volume periods.
        .order_by(desc(ActivityLog.created_at))
        .limit(50000)
    )).all()

    def _bucket():
        return {
            "events": 0,
            "events_with_cache_read": 0,
            "events_with_cache_creation": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "new_input_tokens": 0,
        }

    overall = _bucket()
    by_group: dict[str, dict] = {}
    # v3.0.80: bucketed time series. Key = bucket-aligned epoch second
    # (UTC). When bucket_minutes is 0 we skip this dict entirely so the
    # default codepath stays cheap.
    time_buckets: dict[int, dict] = {}
    bucket_secs = bucket_minutes * 60

    for em, pid, kid, ts in rows:
        try:
            m = em if isinstance(em, dict) else json.loads(em or "{}")
        except (TypeError, ValueError):
            continue
        cr = int(m.get("cache_read_input_tokens") or 0)
        cc = int(m.get("cache_creation_input_tokens") or 0)
        it = int(m.get("in_tok") or 0)
        overall["events"] += 1
        overall["new_input_tokens"] += it
        if cr:
            overall["events_with_cache_read"] += 1
            overall["cache_read_tokens"] += cr
        if cc:
            overall["events_with_cache_creation"] += 1
            overall["cache_creation_tokens"] += cc
        if group_by != "none":
            key = pid if group_by == "provider" else kid
            if key:
                b = by_group.setdefault(key, _bucket())
                b["events"] += 1
                b["new_input_tokens"] += it
                if cr:
                    b["events_with_cache_read"] += 1
                    b["cache_read_tokens"] += cr
                if cc:
                    b["events_with_cache_creation"] += 1
                    b["cache_creation_tokens"] += cc
        if bucket_secs > 0 and ts is not None:
            # Server stores naive UTC datetimes; coerce to epoch via the
            # tz-aware path so floor-by-bucket-size is correct.
            try:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                epoch = int(ts.timestamp())
            except Exception:
                continue
            bucket_key = (epoch // bucket_secs) * bucket_secs
            tb = time_buckets.setdefault(bucket_key, _bucket())
            tb["events"] += 1
            tb["new_input_tokens"] += it
            if cr:
                tb["events_with_cache_read"] += 1
                tb["cache_read_tokens"] += cr
            if cc:
                tb["events_with_cache_creation"] += 1
                tb["cache_creation_tokens"] += cc

    # Resolve display names for the grouped keys.
    name_lookup: dict[str, str] = {}
    if group_by == "provider" and by_group:
        ps = (await db.execute(
            select(Provider).where(Provider.id.in_(list(by_group.keys())))
        )).scalars().all()
        name_lookup = {p.id: p.name for p in ps}
    elif group_by == "api_key" and by_group:
        ks = (await db.execute(
            select(ApiKey).where(ApiKey.id.in_(list(by_group.keys())))
        )).scalars().all()
        name_lookup = {k.id: k.name for k in ks}

    def _enrich(b: dict) -> dict:
        n = b["events"] or 1
        out = dict(b)
        out["cache_hit_rate_pct"] = round(100.0 * b["events_with_cache_read"] / n, 2)
        denom = b["new_input_tokens"] + b["cache_read_tokens"]
        out["cache_share_of_input_pct"] = (
            round(100.0 * b["cache_read_tokens"] / denom, 2) if denom else 0.0
        )
        out["estimated_savings_usd"] = round(
            (b["cache_read_tokens"] / 1_000_000.0) * rate_per_million * cache_discount_pct,
            4,
        )
        return out

    grouped_out: list[dict] = []
    for key, b in sorted(by_group.items(), key=lambda kv: -kv[1]["events"]):
        e = _enrich(b)
        e["id"] = key
        e["name"] = name_lookup.get(key, key)
        grouped_out.append(e)

    # v3.0.80: assemble the time-series array sorted oldest→newest so
    # consumers can chart it directly without re-sorting. Empty buckets
    # are filled in for visual continuity (otherwise sparse traffic
    # produces gappy charts).
    time_series: list[dict] = []
    if bucket_secs > 0 and time_buckets:
        first_bucket = ((int(cutoff.timestamp()) // bucket_secs) + 1) * bucket_secs
        last_bucket = (int(datetime.now(timezone.utc).timestamp()) // bucket_secs) * bucket_secs
        for bk in range(first_bucket, last_bucket + bucket_secs, bucket_secs):
            b = time_buckets.get(bk, _bucket())
            entry = _enrich(b)
            entry["bucket_start"] = datetime.fromtimestamp(bk, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            entry["bucket_end"] = datetime.fromtimestamp(bk + bucket_secs, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            time_series.append(entry)

    response: dict = {
        "window_minutes": window_minutes,
        "rate_per_million_usd": rate_per_million,
        "cache_discount_pct": cache_discount_pct,
        "overall": _enrich(overall),
        "by_group": grouped_out,
        "group_by": group_by,
    }
    if bucket_minutes > 0:
        response["bucket_minutes"] = bucket_minutes
        response["time_series"] = time_series
    return response


# v3.0.76 — Per-caller usage CSV export. Evergreen reporting hook —
# operator can download a monthly rollup for billing-back to internal
# teams or external customer chargeback. Default window 7 days, group
# by api_key (most common reporting pivot). CSV shape is intentionally
# wide (one row per group, all metrics) so it imports cleanly into
# spreadsheets / BI tools without further reshaping.
@router.get("/usage-report.csv")
async def usage_report_csv(
    window_minutes: int = Query(
        10080, ge=1, le=43200,
        description="rolling window (default 7 days = 10080 min; max 30 days = 43200 min)",
    ),
    group_by: str = Query(
        "api_key", description="'api_key' (default) or 'provider'",
    ),
    rate_per_million: float = Query(
        3.0,
        description="Per-million-token rate for cache-savings estimate "
        "(default 3.0 ≈ Claude Sonnet 4.6 input).",
    ),
    cache_discount_pct: float = Query(
        0.9, ge=0.0, le=1.0,
        description="Cache-read discount fraction (default 0.9 = Anthropic's 90%).",
    ),
    _: AdminUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """CSV download of per-caller usage and cache savings over a window.

    Columns: id, name, events, in_tokens, out_tokens,
    cache_read_tokens, cache_creation_tokens, cost_usd,
    quota_usd_subscription, estimated_cache_savings_usd.

    Reads the same event_meta fields the cache-stats endpoint does
    plus ``in_tok``, ``out_tok``, ``cost_usd``, ``quota_usd``. Subscription
    providers (claude-oauth, codex-oauth) report cost_usd=0 + quota_usd
    populated — both columns are surfaced so reports can show "real $"
    vs "subscription $" separately.
    """
    import csv
    import io
    import json
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select, desc

    from app.models.db import Provider, ApiKey

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    rows = (await db.execute(
        select(
            ActivityLog.event_meta, ActivityLog.provider_id, ActivityLog.api_key_id,
        )
        .where(ActivityLog.created_at >= cutoff)
        .where(ActivityLog.event_type == "llm_request")
        .where(ActivityLog.severity == "info")
        # Same 50k cap as cache-stats; for 30-day windows operator should
        # query the underlying activity_log directly if they need full
        # fidelity. This endpoint is for typical billing-cycle rollups.
        .order_by(desc(ActivityLog.created_at))
        .limit(50000)
    )).all()

    def _bucket():
        return {
            "events": 0,
            "in_tokens": 0,
            "out_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_usd": 0.0,
            "quota_usd": 0.0,
        }

    by_group: dict[str, dict] = {}

    for em, pid, kid in rows:
        try:
            m = em if isinstance(em, dict) else json.loads(em or "{}")
        except (TypeError, ValueError):
            continue
        key = pid if group_by == "provider" else kid
        if not key:
            continue
        b = by_group.setdefault(key, _bucket())
        b["events"] += 1
        b["in_tokens"] += int(m.get("in_tok") or 0)
        b["out_tokens"] += int(m.get("out_tok") or 0)
        b["cache_read_tokens"] += int(m.get("cache_read_input_tokens") or 0)
        b["cache_creation_tokens"] += int(m.get("cache_creation_input_tokens") or 0)
        try:
            b["cost_usd"] += float(m.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            b["quota_usd"] += float(m.get("quota_usd") or 0.0)
        except (TypeError, ValueError):
            pass

    name_lookup: dict[str, str] = {}
    if group_by == "provider" and by_group:
        ps = (await db.execute(
            select(Provider).where(Provider.id.in_(list(by_group.keys())))
        )).scalars().all()
        name_lookup = {p.id: p.name for p in ps}
    elif group_by == "api_key" and by_group:
        ks = (await db.execute(
            select(ApiKey).where(ApiKey.id.in_(list(by_group.keys())))
        )).scalars().all()
        name_lookup = {k.id: k.name for k in ks}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "name", "events", "in_tokens", "out_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "cost_usd", "quota_usd_subscription",
        "estimated_cache_savings_usd",
    ])
    # Sort by events desc — heaviest callers at top
    for key, b in sorted(by_group.items(), key=lambda kv: -kv[1]["events"]):
        savings = (b["cache_read_tokens"] / 1_000_000.0) * rate_per_million * cache_discount_pct
        writer.writerow([
            key,
            name_lookup.get(key, key),
            b["events"],
            b["in_tokens"],
            b["out_tokens"],
            b["cache_read_tokens"],
            b["cache_creation_tokens"],
            f"{b['cost_usd']:.6f}",
            f"{b['quota_usd']:.6f}",
            f"{savings:.4f}",
        ])

    csv_bytes = buf.getvalue()
    days = window_minutes // 1440
    fname_suffix = f"{days}d" if days >= 1 else f"{window_minutes}m"
    filename = f"usage-report-{group_by}-{fname_suffix}.csv"
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
