"""v3.7.32 (#252 phase 5) — admin endpoints for the AI provider supervisor.

Mirrors ``app/api/ai_rate_limiter.py`` structure: list / apply / revert /
dismiss / trigger-now.

All endpoints admin-gated. The apply/revert/dismiss endpoints respect
manual_override_until — they refuse to mutate a locked provider, returning
409 Conflict with a clear message.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import Provider, ProviderAiReview

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/providers", tags=["ai-provider-supervisor"])


async def _get_provider_or_404(db: AsyncSession, provider_id: str) -> Provider:
    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    p = rs.scalar_one_or_none()
    if not p or p.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    return p


async def _get_review_or_404(
    db: AsyncSession, provider_id: str, review_id: int,
) -> ProviderAiReview:
    rs = await db.execute(
        select(ProviderAiReview)
        .where(ProviderAiReview.id == review_id)
        .where(ProviderAiReview.provider_id == provider_id)
    )
    r = rs.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Review not found")
    return r


def _serialize_review(r: ProviderAiReview) -> dict:
    return {
        "id": r.id,
        "provider_id": r.provider_id,
        "captured_at": r.captured_at.isoformat() if r.captured_at else None,
        "llm_model": r.llm_model,
        "llm_verdict": r.llm_verdict,
        "llm_reasoning": r.llm_reasoning,
        "suggested_priority_delta": r.suggested_priority_delta,
        "suggested_auto_skip_hours": r.suggested_auto_skip_hours,
        "stats_summary": r.stats_summary,
        "applied_at": r.applied_at.isoformat() if r.applied_at else None,
        "applied_action": r.applied_action,
        "prior_priority": r.prior_priority,
        "prior_auto_skip_until": r.prior_auto_skip_until.isoformat() if r.prior_auto_skip_until else None,
        "reverted_at": r.reverted_at.isoformat() if r.reverted_at else None,
        "dismissed_at": r.dismissed_at.isoformat() if r.dismissed_at else None,
    }


@router.get("/{provider_id}/ai-reviews")
async def list_reviews(
    provider_id: str,
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Return the most recent N supervisor reviews for a provider."""
    await _get_provider_or_404(db, provider_id)
    rs = await db.execute(
        select(ProviderAiReview)
        .where(ProviderAiReview.provider_id == provider_id)
        .order_by(desc(ProviderAiReview.captured_at))
        .limit(limit)
    )
    return [_serialize_review(r) for r in rs.scalars().all()]


@router.get("/{provider_id}/ai-supervisor-stats")
async def get_current_stats(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Diagnostic endpoint: return the live stats that would be fed to
    the LLM right now. Useful when the operator wants to inspect what
    the supervisor sees without firing an LLM call."""
    await _get_provider_or_404(db, provider_id)
    from app.config import settings
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats
    short_min = int(getattr(settings, "ai_provider_supervisor_short_window_min", 30))
    long_days = int(getattr(settings, "ai_provider_supervisor_trend_window_days", 1))
    return await compute_provider_stats(
        db, provider_id,
        short_window_min=short_min,
        long_window_days=long_days,
    )


@router.post("/{provider_id}/ai-reviews/{review_id}/apply")
async def apply_review(
    provider_id: str,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Manually apply a review's verdict to the live provider. Used when
    auto-apply is OFF and the operator wants to act on a single review.

    Refuses with 409 if the provider has manual_override_until set —
    operator must explicitly Enable the provider first to clear the lock."""
    provider = await _get_provider_or_404(db, provider_id)
    review = await _get_review_or_404(db, provider_id, review_id)
    if review.applied_at is not None:
        raise HTTPException(400, "Review already applied")
    if review.dismissed_at is not None:
        raise HTTPException(400, "Review was dismissed")
    if getattr(provider, "manual_override_until", None) is not None:
        raise HTTPException(
            409,
            "Provider is under manual override — release the lock first via "
            "the Enable button or the top-of-page banner",
        )
    from app.monitoring.ai_provider_supervisor import _apply_suggestion
    await _apply_suggestion(
        db, provider, review,
        {
            "verdict": review.llm_verdict,
            "suggested_priority_delta": review.suggested_priority_delta,
            "suggested_auto_skip_hours": review.suggested_auto_skip_hours,
            "reasoning": review.llm_reasoning,
        },
    )
    await db.commit()
    return _serialize_review(review)


@router.post("/{provider_id}/ai-reviews/{review_id}/dismiss")
async def dismiss_review(
    provider_id: str,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Mark a review as dismissed without applying. Audit trail only —
    doesn't mutate the provider. The dismissed flag prevents the operator
    from accidentally clicking apply on the same review later."""
    review = await _get_review_or_404(db, provider_id, review_id)
    if review.dismissed_at is not None:
        return _serialize_review(review)  # idempotent
    if review.applied_at is not None:
        raise HTTPException(400, "Cannot dismiss a review that was already applied")
    review.dismissed_at = datetime.utcnow()
    await db.commit()
    return _serialize_review(review)


@router.post("/{provider_id}/ai-reviews/{review_id}/revert")
async def revert_review(
    provider_id: str,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Undo an applied review: restore prior_priority or
    prior_auto_skip_until. Records reverted_at so the lifecycle is
    auditable.

    Note: revert does NOT clear manual_override_until — that's
    operator-controlled separately. If the operator manually-disabled
    the provider AFTER the supervisor disabled it, reverting only
    undoes the supervisor's change; the manual lock stays.
    """
    provider = await _get_provider_or_404(db, provider_id)
    review = await _get_review_or_404(db, provider_id, review_id)
    if review.applied_at is None:
        raise HTTPException(400, "Review was never applied")
    if review.reverted_at is not None:
        raise HTTPException(400, "Review already reverted")

    action = review.applied_action or ""
    if action.startswith("priority+="):
        if review.prior_priority is not None:
            provider.priority = review.prior_priority
    elif action.startswith("auto_skip+="):
        provider.auto_skip_until = review.prior_auto_skip_until
        provider.auto_skip_reason = None
    review.reverted_at = datetime.utcnow()
    await db.commit()
    return _serialize_review(review)


@router.post("/{provider_id}/ai-supervisor-trigger")
async def trigger_review_now(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Fire one supervisor review immediately for this provider —
    bypasses the 30-min cadence. Useful for smoke-testing the supervisor
    after configuration changes. Respects manual_override_until."""
    provider = await _get_provider_or_404(db, provider_id)
    if getattr(provider, "manual_override_until", None) is not None:
        raise HTTPException(
            409,
            "Provider is under manual override — supervisor will not "
            "review until the lock is released",
        )
    from app.monitoring.ai_provider_supervisor import review_one_provider
    result = await review_one_provider(db, provider)
    if result is None:
        return {"ok": False, "reason": "no_traffic_or_classifier_unavailable"}
    return {"ok": True, **result}
