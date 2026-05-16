"""v3.7.30 (#252 phase 3) — stats compute for the AI provider supervisor.

Given a Provider id and two windows (short = 30 min, long = trend
baseline e.g. 1d), returns a dict the supervisor worker (Phase 4)
feeds to the LLM for classification. Defensive — every field is
optional so an LLM verdict can still be computed even when some
signals are missing.

Signal sources:
  - activity_log: per-request data (latency_ms, in_tok/out_tok,
    cost_usd, severity, error_class). Filter by provider_id.
  - hedging.py: in-memory TTFT samples + PeakEWMA per provider.
  - provider_metrics table (existing): rolled-up successes/failures.

The function intentionally returns serializable primitives so the
output can land directly in ``ProviderAiReview.stats_summary`` (JSON
column) for diagnostic replay.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def _window_stats(
    db: AsyncSession, provider_id: str, since: datetime,
) -> dict:
    """Aggregate activity_log rows for one window. Returns counts,
    sums, and severity / error-class breakdowns."""
    from app.models.db import ActivityLog
    cutoff = str(since)
    rows = (await db.execute(
        select(
            ActivityLog.severity,
            ActivityLog.event_meta,
        )
        .where(ActivityLog.provider_id == provider_id)
        .where(ActivityLog.event_type == "llm_request")
        .where(ActivityLog.created_at >= cutoff)
    )).all()

    n = 0
    n_warn = 0
    n_err = 0
    in_tok_sum = 0
    out_tok_sum = 0
    cost_sum = 0.0
    latency_ms_sum = 0.0
    out_tok_samples: list[int] = []
    latency_samples: list[float] = []
    err_classes: Counter = Counter()
    for severity, meta in rows:
        try:
            m = json.loads(meta) if isinstance(meta, str) else (meta or {})
        except Exception:
            m = {}
        # v3.10.14 BUG-026 — exclude internal-source traffic (the AI
        # supervisor's own /v1/messages classifier calls, tagged by
        # record_outcome with event_meta.internal_source) from the
        # stats that drive the supervisor's verdicts. Otherwise the
        # supervisor counts its own calls against the provider it judges.
        if m.get("internal_source"):
            continue
        n += 1
        if severity == "warning":
            n_warn += 1
        elif severity == "error":
            n_err += 1
        in_tok_sum += int(m.get("in_tok") or 0)
        out_tok = int(m.get("out_tok") or 0)
        out_tok_sum += out_tok
        if out_tok > 0:
            out_tok_samples.append(out_tok)
        cost_sum += float(m.get("cost_usd") or 0)
        lat = m.get("latency_ms")
        if isinstance(lat, (int, float)) and lat > 0:
            latency_ms_sum += float(lat)
            latency_samples.append(float(lat))
        ec = m.get("error_class")
        if ec:
            err_classes[ec] += 1

    success_rate = None
    if n > 0:
        success_rate = round(100.0 * (n - n_warn - n_err) / n, 1)
    avg_latency_ms = (latency_ms_sum / len(latency_samples)) if latency_samples else None
    avg_out_tok = (out_tok_sum / n) if n > 0 else None
    p95_latency_ms = None
    if len(latency_samples) >= 5:
        latency_samples.sort()
        p95_latency_ms = round(latency_samples[int(len(latency_samples) * 0.95)], 1)
    return {
        "requests": n,
        "warnings": n_warn,
        "errors": n_err,
        "success_rate_pct": success_rate,
        "in_tok_total": in_tok_sum,
        "out_tok_total": out_tok_sum,
        "avg_out_tok": round(avg_out_tok, 1) if avg_out_tok is not None else None,
        "cost_usd_total": round(cost_sum, 4),
        "avg_cost_per_req_usd": round(cost_sum / n, 6) if n > 0 else None,
        "avg_latency_ms": round(avg_latency_ms, 1) if avg_latency_ms is not None else None,
        "p95_latency_ms": p95_latency_ms,
        "error_class_breakdown": dict(err_classes),
    }


async def compute_provider_stats(
    db: AsyncSession,
    provider_id: str,
    *,
    short_window_min: int = 30,
    long_window_days: int = 1,
) -> dict:
    """Build the full stats dict the supervisor's LLM call consumes.

    Output shape:
      {
        "provider_id": "...",
        "short_window": { ... aggregate stats over last `short_window_min` ... },
        "long_window":  { ... aggregate stats over last `long_window_days` ... },
        "trend": {
          "request_volume_ratio": short/long-normalized-by-time,
          "success_rate_delta_pct": short - long,
          "avg_latency_delta_pct": (short - long) / long * 100,
          "cost_per_req_delta_pct": same,
          "avg_out_tok_delta_pct": same,
        },
        "ttft": {
          "peak_ewma_ms": <hedging.py value>,
          "p95_ms": <hedging.py value>,
        },
        "captured_at": ISO8601 UTC,
      }

    Returns an empty-ish dict if the provider has no activity in
    either window — the supervisor should write `verdict=normal` /
    skip the LLM call in that case (saves cost on idle providers).
    """
    now = datetime.utcnow()
    short_since = now - timedelta(minutes=short_window_min)
    long_since = now - timedelta(days=long_window_days)

    short = await _window_stats(db, provider_id, short_since)
    long_ = await _window_stats(db, provider_id, long_since)

    # Trend deltas — only computed when long-window has enough samples.
    trend: dict = {}
    if long_["requests"] >= 5:
        # Normalize request volume: short window is 30/1440 = 0.0208 of
        # long (when long=1d). If short request rate equals long rate,
        # ratio == 1.0; >1.0 means traffic is spiking, <1.0 means quieting.
        ratio_norm = (short_window_min / 60.0) / (long_window_days * 24.0)
        expected_short = long_["requests"] * ratio_norm
        if expected_short > 0:
            trend["request_volume_ratio"] = round(short["requests"] / expected_short, 2)

        if short["success_rate_pct"] is not None and long_["success_rate_pct"] is not None:
            trend["success_rate_delta_pct"] = round(
                short["success_rate_pct"] - long_["success_rate_pct"], 1,
            )

        def _delta(s_key: str) -> Optional[float]:
            s = short.get(s_key)
            l = long_.get(s_key)
            if s is None or l is None or l == 0:
                return None
            return round((s - l) / l * 100.0, 1)

        trend["avg_latency_delta_pct"] = _delta("avg_latency_ms")
        trend["cost_per_req_delta_pct"] = _delta("avg_cost_per_req_usd")
        trend["avg_out_tok_delta_pct"] = _delta("avg_out_tok")

    # In-memory TTFT from the hedging module — captures fine-grained
    # per-request first-token latency that the activity_log's
    # whole-request latency_ms misses.
    ttft: dict = {}
    try:
        from app.routing.hedging import peak_ewma, provider_p95_ms
        ttft_peak = peak_ewma(provider_id)
        ttft_p95 = provider_p95_ms(provider_id)
        if ttft_peak is not None:
            ttft["peak_ewma_ms"] = round(ttft_peak, 1)
        if ttft_p95 is not None:
            ttft["p95_ms"] = round(ttft_p95, 1)
    except Exception:
        pass

    return {
        "provider_id": provider_id,
        "short_window": {
            "window_minutes": short_window_min,
            **short,
        },
        "long_window": {
            "window_days": long_window_days,
            **long_,
        },
        "trend": trend,
        "ttft": ttft,
        "captured_at": now.isoformat() + "Z",
    }
