"""v3.7.10 — proactive AI rate limiter.

Operator request 2026-05-10 (Q5 of LMRHv2 design discussion):
> we need an ai built into the proxy that itself reviews this and
> proactively makes suggestions - so default to a loose rate limit
> but then that AI should analyze rates and the traffic it is using
> and if there's red flags; proactively slow that api key's usage
> or it's source ip.

Architecture: a background worker scans each enabled api_key's
last N minutes of activity every ``ai_rate_limiter_interval_sec``
(default 300s = 5min, configurable). For each key with recent
traffic:

  1. Compute a structured stats summary (req-rate, error-rate,
     latency p50/p95, prompt-size variance, IP variance, etc.).
  2. Pull 2-3 sample ``request_preview`` snippets — redacted of
     anything that looks like a token — to give the LLM context.
  3. Call the configured model via the proxy itself (so we reuse
     the existing routing + budget logic). Request structured
     JSON output: ``{verdict, reasoning}``.
  4. Write an ``ApiKeyAiReview`` row.
  5. If ``ai_rate_limiter_auto_apply=True`` AND verdict in
     ``{throttle, block}``: apply the suggested action and record
     ``prior_rate_limit_rpm`` so the operator can revert.

Defaults to **opt-in per node** (``ai_rate_limiter_enabled=False``)
and **suggest-only** (``ai_rate_limiter_auto_apply=False``). Operator
flips one node at a time to validate, then per-node enables auto-apply.

The IP-blocking action is **deferred to v3.7.11** — we'd need new
middleware infrastructure to enforce IP blocks, and the operator
explicitly approved deferring that scope.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 300
WARMUP_DELAY_SEC = 90
DEFAULT_WINDOW_MIN = 30
_TASK: Optional[asyncio.Task] = None

# Patterns we redact from sample previews before sending to the LLM
# (don't leak API keys / tokens / secrets through the analysis path).
_REDACT_PATTERNS = [
    (re.compile(r"sk-ant-(?:oat|api)\d*-[\w-]+", re.I), "<REDACTED-ANTHROPIC-TOKEN>"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}", re.I), "<REDACTED-OPENAI-TOKEN>"),
    (re.compile(r"llmp-[A-Za-z0-9_-]{20,}"), "<REDACTED-LLMP-KEY>"),
    (re.compile(r"AIza[A-Za-z0-9_-]{35}"), "<REDACTED-GOOGLE-KEY>"),
    (re.compile(r'"api_key"\s*:\s*"[^"]+"', re.I), '"api_key": "<REDACTED>"'),
]


def _redact(text: str) -> str:
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text[:300]  # also cap length


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * pct)))
    return s[idx]


def compute_stats(events: list[dict]) -> dict:
    """Compute the per-key stats summary the LLM sees.

    Only includes aggregate statistics — never raw request bodies or
    full responses (those leak workload semantics). Sample previews
    are kept separately and redacted.

    Counts:
      - total requests, error count, unique IPs, unique models
    Rates / distributions:
      - req-rate per minute, error rate pct
      - p50/p95 input tokens, output tokens, latency
      - cost-class distribution (subscription / per_call)
      - prompt-size variance pct
    v3.7.12 — top source IPs by request count (max 5). Lets the LLM
    recommend a specific ``verdict=block_ip`` when one source IP is
    the abuser and the rest of the key's traffic is legitimate.
    """
    out: dict = {
        "total_requests": len(events),
        "error_count": 0,
        "unique_ips": 0,
        "unique_models": 0,
        "req_rate_per_min": 0.0,
        "error_rate_pct": 0.0,
        "p50_input_tokens": None,
        "p95_input_tokens": None,
        "p50_output_tokens": None,
        "p95_output_tokens": None,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
        "cost_class_dist": {},
        "prompt_size_variance_pct": None,
        "top_source_ips": {},  # v3.7.12 — {ip: count} top 5
    }
    if not events:
        return out
    ips: set = set()
    ip_counts: dict = {}  # v3.7.12 — for top_source_ips
    models: set = set()
    in_toks: list[float] = []
    out_toks: list[float] = []
    lats: list[float] = []
    cc_counts: dict = {}
    errors = 0
    for e in events:
        md = e.get("metadata", {}) or {}
        sev = e.get("severity") or ""
        if sev in ("error", "warning", "critical"):
            errors += 1
        ip = md.get("client_ip")
        if ip:
            ips.add(ip)
            ip_counts[ip] = ip_counts.get(ip, 0) + 1
        m = md.get("model")
        if m:
            models.add(m)
        try:
            in_toks.append(float(md.get("in_tok") or 0))
            out_toks.append(float(md.get("out_tok") or 0))
            lat = md.get("latency_ms")
            if lat:
                lats.append(float(lat))
        except (TypeError, ValueError):
            pass
        cc = md.get("cost_class") or "unknown"
        cc_counts[cc] = cc_counts.get(cc, 0) + 1
    out["error_count"] = errors
    out["unique_ips"] = len(ips)
    out["unique_models"] = len(models)
    # v3.7.12 — top-5 source IPs by request count, sorted desc
    out["top_source_ips"] = dict(
        sorted(ip_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    )
    # Time-window for rate calc: span between first and last event
    try:
        timestamps = sorted([e.get("timestamp", "") for e in events if e.get("timestamp")])
        if len(timestamps) >= 2:
            t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            span_min = max((t1 - t0).total_seconds() / 60.0, 1.0)
            out["req_rate_per_min"] = round(len(events) / span_min, 2)
    except Exception:
        pass
    out["error_rate_pct"] = round(100 * errors / max(len(events), 1), 1)
    out["p50_input_tokens"] = _percentile(in_toks, 0.50)
    out["p95_input_tokens"] = _percentile(in_toks, 0.95)
    out["p50_output_tokens"] = _percentile(out_toks, 0.50)
    out["p95_output_tokens"] = _percentile(out_toks, 0.95)
    out["p50_latency_ms"] = _percentile(lats, 0.50)
    out["p95_latency_ms"] = _percentile(lats, 0.95)
    out["cost_class_dist"] = cc_counts
    # Prompt-size variance: coefficient of variation (std/mean × 100)
    # for input tokens. Sudden shifts often signal a misconfigured caller.
    if in_toks and len(in_toks) >= 5:
        mean = sum(in_toks) / len(in_toks)
        if mean > 0:
            var = sum((x - mean) ** 2 for x in in_toks) / len(in_toks)
            std = var ** 0.5
            out["prompt_size_variance_pct"] = round(100 * std / mean, 1)
    return out


def pick_sample_previews(events: list[dict], n: int = 3) -> list[str]:
    """Pick up to N representative request_preview snippets, redacted.

    Picks: first event, middle event, last event (gives time-spread).
    """
    if not events:
        return []
    out: list[str] = []
    idxs = []
    if len(events) >= 1:
        idxs.append(0)
    if len(events) >= 3:
        idxs.append(len(events) // 2)
    if len(events) >= 2:
        idxs.append(len(events) - 1)
    idxs = sorted(set(idxs))[:n]
    for i in idxs:
        md = events[i].get("metadata", {}) or {}
        rp = md.get("request_preview")
        if isinstance(rp, str):
            out.append(_redact(rp))
    return out


def build_prompt(stats: dict, samples: list[str], api_key_name: str) -> str:
    samples_block = "\n".join(f"  - {s!r}" for s in samples) if samples else "  (none captured)"
    top_ips = stats.get('top_source_ips') or {}
    if top_ips:
        ips_block = "\n".join(f"  - {ip}: {n} requests" for ip, n in top_ips.items())
    else:
        ips_block = "  (no source IPs captured — may be internal traffic)"
    return f"""You are a rate-limit analyst for an LLM proxy. Review the
following 30-minute activity summary for ONE API key and classify
whether the traffic pattern suggests legitimate use, abuse, or
something in between.

API key name: {api_key_name}

Activity summary:
- Total requests: {stats.get('total_requests')}
- Request rate: {stats.get('req_rate_per_min')}/min
- Errors: {stats.get('error_count')} ({stats.get('error_rate_pct')}%)
- Unique source IPs: {stats.get('unique_ips')}
- Unique models requested: {stats.get('unique_models')}
- Input tokens p50/p95: {stats.get('p50_input_tokens')} / {stats.get('p95_input_tokens')}
- Output tokens p50/p95: {stats.get('p50_output_tokens')} / {stats.get('p95_output_tokens')}
- Latency p50/p95: {stats.get('p50_latency_ms')}ms / {stats.get('p95_latency_ms')}ms
- Cost class distribution: {stats.get('cost_class_dist')}
- Prompt-size variance (coefficient of variation): {stats.get('prompt_size_variance_pct')}%

Top source IPs (by request count):
{ips_block}

Sample request previews (redacted):
{samples_block}

Classify into exactly one of:
- "normal" — healthy pattern, no concern
- "watch" — slightly elevated, keep observing
- "throttle" — suspicious (sudden spike, abusive prompts, error storms) — recommend lowering rate_limit_rpm to the configured throttle floor
- "block_ip" — ONE specific source IP is the abuser, the rest of the key's traffic is legitimate — recommend blocking just that IP. Pick the IP from the "Top source IPs" list above.
- "block" — clear key-wide abuse — recommend disabling the entire key

Reply ONLY with valid JSON (no markdown, no preamble):
- For normal/watch/throttle/block: {{"verdict": "...", "reasoning": "<2-3 sentences>"}}
- For block_ip (REQUIRES ip field): {{"verdict": "block_ip", "ip": "<one of the top IPs>", "reasoning": "<...>"}}"""


def parse_llm_response(text: str) -> Optional[dict]:
    """Extract the JSON verdict from the LLM response, tolerating
    markdown wrappers and chatty preambles."""
    if not text:
        return None
    # Try direct parse first
    try:
        d = json.loads(text)
        if isinstance(d, dict) and "verdict" in d:
            return d
    except Exception:
        pass
    # Try to find a JSON block via regex
    m = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", text)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and "verdict" in d:
                return d
        except Exception:
            pass
    return None


def _verdict_to_action(verdict: str) -> str:
    if verdict == "throttle":
        return "throttle_rpm"
    if verdict == "block":
        return "disable"
    if verdict == "block_ip":
        return "block_ip"
    return "none"


async def classify_with_llm(
    stats: dict, samples: list[str], api_key_name: str,
) -> Optional[dict]:
    """Call the proxy's own /v1/messages to classify. Returns dict with
    ``verdict`` + ``reasoning`` or None on failure."""
    try:
        from app.config import settings
    except Exception:
        return None
    api_key = getattr(settings, "ai_rate_limiter_internal_api_key", None)
    model = getattr(settings, "ai_rate_limiter_model", "claude-haiku-4-5-20251001")
    if not api_key:
        logger.info("ai_rate_limiter.no_internal_api_key — set AI_RATE_LIMITER_INTERNAL_API_KEY to enable LLM classification")
        return None
    prompt = build_prompt(stats, samples, api_key_name)
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            # Call our own proxy on localhost so we reuse routing/budget
            # logic AND the call shows up in our own activity log
            # (transparent to the operator).
            resp = await client.post(
                "http://localhost:3000/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 250,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "ai_rate_limiter.llm_http_error status=%d body=%s",
                resp.status_code, resp.text[:200],
            )
            return None
        body = resp.json()
        # Anthropic shape: content[0].text
        content = body.get("content") or []
        if not content:
            return None
        text = content[0].get("text", "") if isinstance(content[0], dict) else ""
        return parse_llm_response(text)
    except Exception as exc:
        logger.warning("ai_rate_limiter.llm_call_failed err=%s", exc)
        return None


async def review_one_key(db, api_key, window_min: int = DEFAULT_WINDOW_MIN) -> Optional[dict]:
    """Run one full review for one api_key. Returns the new review row
    dict (for caller logging) or None if skipped (no traffic)."""
    from sqlalchemy import select, desc
    from app.models.db import ActivityLog, ApiKeyAiReview, ApiKey

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    rs = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.api_key_id == api_key.id)
        .where(ActivityLog.timestamp >= cutoff)
        .order_by(desc(ActivityLog.timestamp))
        .limit(2000)  # cap so a runaway key doesn't OOM us
    )
    rows = rs.scalars().all()
    if not rows:
        return None
    events = [
        {
            "timestamp": r.timestamp.isoformat() if r.timestamp else "",
            "event_type": r.event_type,
            "severity": r.severity,
            "message": r.message,
            "metadata": r.metadata or {},
        }
        for r in rows
    ]
    stats = compute_stats(events)
    samples = pick_sample_previews(events)
    classification = await classify_with_llm(stats, samples, api_key.name or api_key.id)
    if classification is None:
        return None
    verdict = (classification.get("verdict") or "").strip().lower()
    if verdict not in ("normal", "watch", "throttle", "block", "block_ip"):
        verdict = "watch"  # treat parser garbage as cautious-watch
    # v3.7.12 — block_ip needs a valid IP from the top_source_ips list,
    # else demote to "watch" to avoid acting on a hallucinated IP.
    suggested_block_ip: Optional[str] = None
    if verdict == "block_ip":
        candidate = (classification.get("ip") or "").strip()
        top_ips = (stats.get("top_source_ips") or {})
        if candidate and candidate in top_ips:
            suggested_block_ip = candidate
        else:
            verdict = "watch"  # LLM didn't name a valid IP — don't act
    suggested = _verdict_to_action(verdict)
    review = ApiKeyAiReview(
        api_key_id=api_key.id,
        llm_model=getattr(__import__("app.config", fromlist=["settings"]).settings, "ai_rate_limiter_model", None),
        llm_verdict=verdict,
        llm_reasoning=classification.get("reasoning", "")[:2000],
        suggested_action=suggested,
        stats_summary=stats,
        suggested_block_ip=suggested_block_ip,
    )
    db.add(review)
    await db.flush()  # populate review.id
    # Auto-apply when configured and verdict is actionable
    try:
        from app.config import settings
        auto_apply = bool(getattr(settings, "ai_rate_limiter_auto_apply", False))
        floor = int(getattr(settings, "ai_rate_limiter_throttle_floor_rpm", 5))
    except Exception:
        auto_apply, floor = False, 5
    if auto_apply and suggested != "none":
        await _apply_suggestion(db, api_key, review, floor)
    await db.commit()
    return {
        "review_id": review.id,
        "api_key_id": api_key.id,
        "api_key_name": api_key.name,
        "verdict": verdict,
        "suggested_action": suggested,
        "auto_applied": auto_apply and suggested != "none",
        "reasoning": classification.get("reasoning", "")[:200],
        "stats_total_requests": stats.get("total_requests"),
        "stats_error_rate_pct": stats.get("error_rate_pct"),
    }


async def _apply_suggestion(db, api_key, review, floor_rpm: int):
    """Mutate api_key + record on review. Caller commits."""
    review.applied_at = datetime.now(timezone.utc)
    review.applied_action = review.suggested_action
    review.prior_rate_limit_rpm = api_key.rate_limit_rpm
    if review.suggested_action == "throttle_rpm":
        api_key.rate_limit_rpm = floor_rpm
    elif review.suggested_action == "disable":
        api_key.enabled = False
    elif review.suggested_action == "block_ip":
        # v3.7.12 — insert into blocked_ips. Idempotent: if the IP is
        # already blocked we don't create a duplicate row (sqlalchemy
        # would raise on the PK violation otherwise).
        from app.models.db import BlockedIp
        from sqlalchemy import select
        ip = review.suggested_block_ip
        if ip:
            rs = await db.execute(select(BlockedIp).where(BlockedIp.ip == ip))
            if rs.scalar_one_or_none() is None:
                db.add(BlockedIp(
                    ip=ip,
                    reason=f"AI rate limiter: {review.llm_reasoning or '(no reasoning)'}",
                    added_by=f"ai-rate-limiter (review {review.id})",
                ))
            # Invalidate the middleware cache on this node
            try:
                from app.middleware.ip_block import _clear_cache_for_tests
                _clear_cache_for_tests()
            except Exception:
                pass
    logger.warning(
        "ai_rate_limiter.applied verdict=%s key_id=%s prior_rpm=%s block_ip=%s",
        review.llm_verdict, api_key.id, review.prior_rate_limit_rpm,
        review.suggested_block_ip,
    )


async def _scan_all_once() -> int:
    """One sweep across every enabled api_key with ``total_requests > 0``."""
    from sqlalchemy import select
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey

    reviewed = 0
    async with AsyncSessionLocal() as db:
        rs = await db.execute(
            select(ApiKey)
            .where(ApiKey.enabled == True)
            .where(ApiKey.deleted_at.is_(None))
        )
        for k in rs.scalars().all():
            try:
                from app.config import settings
                window = int(getattr(settings, "ai_rate_limiter_window_min", DEFAULT_WINDOW_MIN))
                result = await review_one_key(db, k, window_min=window)
                if result:
                    reviewed += 1
                    logger.info(
                        "ai_rate_limiter.reviewed key=%s verdict=%s",
                        result["api_key_name"], result["verdict"],
                    )
            except Exception as exc:
                logger.warning(
                    "ai_rate_limiter.review_failed key_id=%s err=%s",
                    k.id, exc,
                )
    return reviewed


async def _scan_loop() -> None:
    await asyncio.sleep(WARMUP_DELAY_SEC)
    while True:
        try:
            from app.config import settings
            if not getattr(settings, "ai_rate_limiter_enabled", False):
                await asyncio.sleep(60)
                continue
            interval = int(getattr(settings, "ai_rate_limiter_interval_sec", DEFAULT_INTERVAL_SEC))
            n = await _scan_all_once()
            if n:
                logger.info("ai_rate_limiter.swept reviewed=%d", n)
        except Exception as exc:
            logger.warning("ai_rate_limiter.sweep_failed err=%s", exc)
            interval = DEFAULT_INTERVAL_SEC
        await asyncio.sleep(interval)


def start() -> None:
    """Spawn the periodic AI rate-limiter loop. Idempotent. Worker
    silently no-ops when ``ai_rate_limiter_enabled=False`` so it's
    safe to always start from the FastAPI lifespan hook."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scan_loop(), name="ai-rate-limiter-loop")
    logger.info("ai_rate_limiter.started — opt-in via AI_RATE_LIMITER_ENABLED=true")
