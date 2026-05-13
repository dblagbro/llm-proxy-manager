"""v3.7.31 (#252 phase 4) — AI provider supervisor worker.

Mirror of ``app/monitoring/ai_rate_limiter.py`` but on the provider
side. Runs on a configurable cadence (default 30 min), computes
per-provider stats via ``ai_provider_supervisor_stats.compute_provider_stats``,
sends them to an LLM for classification, and writes a
``ProviderAiReview`` row. When ``ai_provider_supervisor_auto_apply``
is enabled, applies the verdict to ``Provider.priority`` or
``Provider.auto_skip_until`` — but NEVER on providers with
``manual_override_until`` set (Phase 1 escape hatch).

Recursion guard: every LLM call carries ``X-Internal-Source:
ai_provider_supervisor`` so the request's own activity-log row is
filterable. The stats helper does not filter on this — the
supervisor reviews are infrequent (30 min × N providers) and
self-amplification risk is negligible compared to the v3.7.10 AI
rate limiter (5 min × all keys).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

WARMUP_DELAY_SEC = 90
_STARTUP_JITTER_MAX_SEC = 60.0
_TASK: Optional[asyncio.Task] = None

VALID_VERDICTS = ("normal", "watch", "deprioritize", "disable", "investigate")


def _interval_sec() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "ai_provider_supervisor_interval_sec", 1800))
    except Exception:
        return 1800


def _enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "ai_provider_supervisor_enabled", False))
    except Exception:
        return False


def _auto_apply() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "ai_provider_supervisor_auto_apply", False))
    except Exception:
        return False


def build_prompt(provider_name: str, provider_type: str, stats: dict) -> str:
    """Construct the LLM prompt. JSON-output enforced via a tight schema
    instruction so the response is parseable."""
    return f"""You are an SRE-style watcher for an LLM proxy fleet. You're reviewing one
provider and deciding whether anything looks off based on its recent traffic.

Provider name: {provider_name}
Provider type: {provider_type}

Stats (short = recent, long = trailing baseline, trend = deltas):
```json
{json.dumps(stats, indent=2, default=str)}
```

Pick exactly one verdict:
- "normal": healthy traffic pattern; no action needed
- "watch": slightly elevated concern; no action yet, just record
- "deprioritize": recommend lowering routing priority by N (set suggested_priority_delta)
- "disable": recommend skipping this provider for some hours (set suggested_auto_skip_hours, 1-24)
- "investigate": anomaly that the operator should look at manually

Respond ONLY with this JSON shape, no surrounding prose:
{{
  "verdict": "normal|watch|deprioritize|disable|investigate",
  "reasoning": "<2-3 sentences explaining the verdict>",
  "suggested_priority_delta": <int 1-3 or null>,
  "suggested_auto_skip_hours": <int 1-24 or null>
}}

If verdict is "normal" or "watch" or "investigate", both suggested_* fields must be null.
If verdict is "deprioritize", suggested_priority_delta must be set.
If verdict is "disable", suggested_auto_skip_hours must be set.
"""


def parse_llm_response(text: str) -> Optional[dict]:
    """Parse the LLM's JSON output. Returns dict or None on failure."""
    if not text:
        return None
    s = text.strip()
    # Strip code fences if present
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    s = s.strip()
    try:
        data = json.loads(s)
    except Exception:
        return None
    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        return None
    return {
        "verdict": verdict,
        "reasoning": (data.get("reasoning") or "")[:2000],
        "suggested_priority_delta": data.get("suggested_priority_delta"),
        "suggested_auto_skip_hours": data.get("suggested_auto_skip_hours"),
    }


async def classify_with_llm(provider_name: str, provider_type: str, stats: dict) -> Optional[dict]:
    """Call the proxy's own /v1/messages to classify. Returns parsed
    response dict or None on failure."""
    try:
        from app.config import settings
    except Exception:
        return None
    api_key = getattr(settings, "ai_provider_supervisor_internal_api_key", None)
    model = getattr(settings, "ai_provider_supervisor_model", "claude-haiku-4-5-20251001")
    if not api_key:
        logger.info(
            "ai_provider_supervisor.no_internal_api_key — "
            "set AI_PROVIDER_SUPERVISOR_INTERNAL_API_KEY to enable LLM classification",
        )
        return None
    prompt = build_prompt(provider_name, provider_type, stats)
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.post(
                "http://localhost:3000/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    # Recursion guard — these requests are tagged so they
                    # appear in activity_log with internal_source set;
                    # operator dashboards / filters can exclude them.
                    "X-Internal-Source": "ai_provider_supervisor",
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "ai_provider_supervisor.llm_http_error status=%d body=%s",
                resp.status_code, resp.text[:200],
            )
            return None
        body = resp.json()
        content = body.get("content") or []
        if not content:
            return None
        text = content[0].get("text", "") if isinstance(content[0], dict) else ""
        return parse_llm_response(text)
    except Exception as exc:
        logger.warning("ai_provider_supervisor.llm_call_failed err=%s", exc)
        return None


async def review_one_provider(db, provider) -> Optional[dict]:
    """Run one full review for one provider. Returns the new review row
    dict (for caller logging) or None if skipped.

    Skip conditions:
      - manual_override_until is non-null (operator-pinned)
      - provider has zero traffic in the short window (no signal)
    """
    if getattr(provider, "manual_override_until", None) is not None:
        logger.debug(
            "ai_provider_supervisor.skip_manual_override provider_id=%s",
            provider.id,
        )
        return None

    from app.config import settings
    from app.models.db import ProviderAiReview
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats

    short_min = int(getattr(settings, "ai_provider_supervisor_short_window_min", 30))
    long_days = int(getattr(settings, "ai_provider_supervisor_trend_window_days", 1))
    stats = await compute_provider_stats(
        db, provider.id,
        short_window_min=short_min,
        long_window_days=long_days,
    )

    if stats["short_window"]["requests"] == 0:
        logger.debug(
            "ai_provider_supervisor.skip_no_traffic provider_id=%s",
            provider.id,
        )
        return None

    verdict_data = await classify_with_llm(provider.name, provider.provider_type, stats)
    if verdict_data is None:
        return None

    review = ProviderAiReview(
        provider_id=provider.id,
        llm_model=getattr(settings, "ai_provider_supervisor_model", None),
        llm_verdict=verdict_data["verdict"],
        llm_reasoning=verdict_data.get("reasoning"),
        suggested_priority_delta=verdict_data.get("suggested_priority_delta"),
        suggested_auto_skip_hours=verdict_data.get("suggested_auto_skip_hours"),
        stats_summary=stats,
    )
    db.add(review)
    await db.flush()  # populate review.id

    # Auto-apply path
    if _auto_apply() and verdict_data["verdict"] in ("deprioritize", "disable"):
        await _apply_suggestion(db, provider, review, verdict_data)

    await db.commit()
    logger.info(
        "ai_provider_supervisor.reviewed provider=%s verdict=%s applied=%s",
        provider.name, verdict_data["verdict"], review.applied_at is not None,
    )
    return {
        "provider_id": provider.id,
        "review_id": review.id,
        "verdict": verdict_data["verdict"],
        "applied": review.applied_at is not None,
    }


async def _apply_suggestion(db, provider, review, verdict_data: dict) -> None:
    """Apply a verdict to the live Provider row. Caps applied per
    settings to prevent runaway AI-driven mutations.

    Defensive: re-checks manual_override_until inside the apply path
    so a race between the worker's stats fetch and apply doesn't bypass
    a freshly-set lock.
    """
    from datetime import datetime, timedelta
    from app.config import settings

    if getattr(provider, "manual_override_until", None) is not None:
        logger.info(
            "ai_provider_supervisor.apply_skipped_manual_override provider_id=%s",
            provider.id,
        )
        return

    verdict = verdict_data["verdict"]
    now = datetime.utcnow()
    review.applied_at = now

    if verdict == "deprioritize":
        delta = int(verdict_data.get("suggested_priority_delta") or 0)
        cap = int(getattr(settings, "ai_provider_supervisor_max_priority_delta", 2))
        delta = max(1, min(delta, cap))
        review.prior_priority = provider.priority
        provider.priority = (provider.priority or 10) + delta
        review.applied_action = f"priority+={delta}"
    elif verdict == "disable":
        hours = int(verdict_data.get("suggested_auto_skip_hours") or 0)
        cap = int(getattr(settings, "ai_provider_supervisor_max_auto_skip_hours", 24))
        hours = max(1, min(hours, cap))
        review.prior_auto_skip_until = provider.auto_skip_until
        provider.auto_skip_until = now + timedelta(hours=hours)
        provider.auto_skip_reason = f"ai_supervisor: {(verdict_data.get('reasoning') or '')[:200]}"
        review.applied_action = f"auto_skip+={hours}h"


async def _scan_all_once() -> dict:
    """One sweep across all enabled providers. Returns counts dict."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from sqlalchemy import select

    out = {"reviewed": 0, "skipped_locked": 0, "skipped_no_traffic": 0, "skipped_no_llm": 0}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Provider)
            .where(Provider.deleted_at.is_(None))
            .where(Provider.enabled == True)  # noqa: E712
        )
        providers = result.scalars().all()
        for p in providers:
            try:
                r = await review_one_provider(db, p)
                if r is not None:
                    out["reviewed"] += 1
                else:
                    # Distinguish skip reasons for ops visibility
                    if getattr(p, "manual_override_until", None) is not None:
                        out["skipped_locked"] += 1
                    else:
                        out["skipped_no_traffic"] += 1
            except Exception as e:
                logger.warning(
                    "ai_provider_supervisor.review_crashed provider=%s err=%s",
                    p.name, e,
                )
    return out


async def _scan_loop() -> None:
    """Periodic loop. No-op when ``ai_provider_supervisor_enabled=False``."""
    jitter = random.uniform(0.0, _STARTUP_JITTER_MAX_SEC)
    await asyncio.sleep(WARMUP_DELAY_SEC + jitter)
    while True:
        if not _enabled():
            await asyncio.sleep(300)
            continue
        try:
            counts = await _scan_all_once()
            if any(counts.values()):
                logger.info(
                    "ai_provider_supervisor.swept reviewed=%d skipped_locked=%d "
                    "skipped_no_traffic=%d",
                    counts["reviewed"], counts["skipped_locked"],
                    counts["skipped_no_traffic"],
                )
        except Exception as e:
            logger.warning("ai_provider_supervisor.sweep_failed err=%s", e)
        await asyncio.sleep(_interval_sec())


def start() -> None:
    """Spawn the supervisor loop. Idempotent. No-op behavior when
    feature flag is off (worker still runs, just sleeps without
    doing anything)."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scan_loop(), name="ai-provider-supervisor-loop")
    logger.info(
        "ai_provider_supervisor.started — opt-in via AI_PROVIDER_SUPERVISOR_ENABLED=true",
    )
