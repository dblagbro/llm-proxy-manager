"""v3.7.10 — admin endpoints for the proactive AI rate limiter.

Three lifecycle actions on each ApiKeyAiReview row:
  - dismiss: operator says "false positive, don't apply"
  - apply:   operator force-applies the suggestion (when auto_apply=False)
  - revert:  operator restores the api_key's prior rate_limit_rpm / enabled

Plus a list endpoint per api_key.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import ApiKey, ApiKeyAiReview

router = APIRouter(prefix="/api/keys", tags=["ai-rate-limiter"])


def _serialize_review(r: ApiKeyAiReview) -> dict:
    return {
        "id": r.id,
        "api_key_id": r.api_key_id,
        "captured_at": r.captured_at.isoformat() if r.captured_at else None,
        "llm_model": r.llm_model,
        "llm_verdict": r.llm_verdict,
        "llm_reasoning": r.llm_reasoning,
        "suggested_action": r.suggested_action,
        "suggested_block_ip": r.suggested_block_ip,  # v3.7.12
        "stats_summary": r.stats_summary,
        "applied_at": r.applied_at.isoformat() if r.applied_at else None,
        "applied_action": r.applied_action,
        "prior_rate_limit_rpm": r.prior_rate_limit_rpm,
        "reverted_at": r.reverted_at.isoformat() if r.reverted_at else None,
        "dismissed_at": r.dismissed_at.isoformat() if r.dismissed_at else None,
    }


@router.get("/{api_key_id}/ai-reviews")
async def list_reviews(
    api_key_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """List recent AI reviews for a single api_key, newest first.

    Includes dismissed / applied / reverted rows so operator sees the
    full history. Filter client-side if you want only-pending.
    """
    rs0 = await db.execute(select(ApiKey).where(ApiKey.id == api_key_id))
    if rs0.scalar_one_or_none() is None:
        raise HTTPException(404, "API key not found")
    rs = await db.execute(
        select(ApiKeyAiReview)
        .where(ApiKeyAiReview.api_key_id == api_key_id)
        .order_by(desc(ApiKeyAiReview.captured_at))
        .limit(limit)
    )
    return [_serialize_review(r) for r in rs.scalars().all()]


@router.post("/{api_key_id}/ai-reviews/{review_id}/dismiss")
async def dismiss_review(
    api_key_id: str,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Operator marks a review as false-positive. Doesn't undo any
    auto-applied action (use ``/revert`` for that)."""
    review = await _load_review(db, api_key_id, review_id)
    if review.dismissed_at:
        return {"ok": True, "already_dismissed": True, "review": _serialize_review(review)}
    review.dismissed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "review": _serialize_review(review)}


@router.post("/{api_key_id}/ai-reviews/{review_id}/apply")
async def apply_review(
    api_key_id: str,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Force-apply the suggestion now (typical path when
    ``ai_rate_limiter_auto_apply=False``). Lowers rate_limit_rpm to the
    configured floor, or sets enabled=False for 'block' verdicts.

    Idempotent: re-applying an already-applied review is a no-op.
    """
    review = await _load_review(db, api_key_id, review_id)
    if review.applied_at:
        return {"ok": True, "already_applied": True, "review": _serialize_review(review)}
    if review.suggested_action == "none":
        raise HTTPException(
            400, "review has no actionable suggestion (verdict was normal/watch)",
        )
    rs = await db.execute(select(ApiKey).where(ApiKey.id == api_key_id))
    api_key = rs.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(404, "API key not found")
    from app.config import settings
    floor = int(getattr(settings, "ai_rate_limiter_throttle_floor_rpm", 5))
    from app.monitoring.ai_rate_limiter import _apply_suggestion
    await _apply_suggestion(db, api_key, review, floor)
    await db.commit()
    return {"ok": True, "review": _serialize_review(review)}


@router.post("/{api_key_id}/ai-reviews/{review_id}/revert")
async def revert_review(
    api_key_id: str,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Undo an applied review. Restores the api_key's prior
    ``rate_limit_rpm`` (for throttle-rpm) or re-enables the key
    (for disable). Records ``reverted_at`` on the review row."""
    review = await _load_review(db, api_key_id, review_id)
    if not review.applied_at:
        raise HTTPException(400, "review has not been applied — nothing to revert")
    if review.reverted_at:
        return {"ok": True, "already_reverted": True, "review": _serialize_review(review)}
    rs = await db.execute(select(ApiKey).where(ApiKey.id == api_key_id))
    api_key = rs.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(404, "API key not found")
    if review.applied_action == "throttle_rpm":
        api_key.rate_limit_rpm = review.prior_rate_limit_rpm
    elif review.applied_action == "disable":
        api_key.enabled = True
    elif review.applied_action == "block_ip":
        # v3.7.12 — remove the IP from the block list. Idempotent
        # (DELETE WHERE ... is a no-op if the row was already removed
        # via the admin endpoint).
        from sqlalchemy import delete as _delete
        from app.models.db import BlockedIp
        if review.suggested_block_ip:
            await db.execute(
                _delete(BlockedIp).where(BlockedIp.ip == review.suggested_block_ip)
            )
            try:
                from app.middleware.ip_block import _clear_cache_for_tests
                _clear_cache_for_tests()
            except Exception:
                pass
    review.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "review": _serialize_review(review)}


async def _load_review(db: AsyncSession, api_key_id: str, review_id: int) -> ApiKeyAiReview:
    rs = await db.execute(
        select(ApiKeyAiReview)
        .where(ApiKeyAiReview.id == review_id)
        .where(ApiKeyAiReview.api_key_id == api_key_id)
    )
    review = rs.scalar_one_or_none()
    if review is None:
        raise HTTPException(404, "review not found for this api_key")
    return review
