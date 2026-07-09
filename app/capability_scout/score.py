"""v5.10.0 — Per-caller capability-suggestion score table I/O.

The score is the gating signal for ``X-Proxy-MCP-Suggestion`` header
emission. Scout writes an activity_log row PER refusal it sees (already
existing v5.7.6 behavior) AND bumps a row in ``caller_capability_score``
keyed by (api_key_id, suggested_tool). When the max score for a caller
crosses a configurable threshold (default 50 = 0.5 in the design doc
notation, ≈ ~3 consecutive refusals at +20 per bump), the response
middleware emits the suggestion header.

Score is stored ``×100 as integer`` for clean cluster-sync diffs —
floats drift across SQLite nodes at the LSB.

Decay: a 6h background worker multiplies every row's score by ~0.96
per tick (= 0.85^(0.25) ≈ 0.9605), so the half-life is ~24h. Rows
that drop below 5 are deleted to keep the table small.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import CallerCapabilityScore

logger = logging.getLogger(__name__)


BUMP_AMOUNT = 20         # +0.2 in design-doc notation
SCORE_CAP = 100          # 1.0 in design-doc notation
DEFAULT_THRESHOLD = 50   # 0.5 in design-doc notation, ≈ ~3 refusals
GC_BELOW = 5             # decay-worker prunes rows under this


async def _threshold() -> int:
    """Settings-driven gate. Operator can raise (quieter) or lower
    (more aggressive). Default 50 = 0.5 — chosen by operator interview
    2026-06-30."""
    try:
        from app.config import settings
        return int(getattr(settings, "mcp_suggestion_threshold", DEFAULT_THRESHOLD) or DEFAULT_THRESHOLD)
    except Exception:
        return DEFAULT_THRESHOLD


async def is_emission_enabled() -> bool:
    """Master switch. Default ON because v5.10 Ship 1+2 is a behavior
    change we want observable from the start; operator can flip it
    settings-side if it becomes a problem."""
    try:
        from app.config import settings
        return bool(getattr(settings, "mcp_suggestion_emission_enabled", True))
    except Exception:
        return True


async def bump_score(
    db: AsyncSession,
    api_key_id: str,
    suggested_tool: str,
) -> int:
    """Increment the (api_key_id, suggested_tool) score by BUMP_AMOUNT
    (capped at SCORE_CAP). Returns the new score. Failures are
    swallowed so the scout NEVER breaks the response path; logged at
    DEBUG so the operator can grep if scores stop accumulating
    unexpectedly.
    """
    if not api_key_id or not suggested_tool:
        return 0
    try:
        from datetime import datetime
        now = datetime.utcnow()
        rs = await db.execute(
            select(CallerCapabilityScore)
            .where(CallerCapabilityScore.api_key_id == api_key_id)
            .where(CallerCapabilityScore.suggested_tool == suggested_tool)
            .limit(1)
        )
        row = rs.scalar_one_or_none()
        if row is None:
            row = CallerCapabilityScore(
                api_key_id=api_key_id,
                suggested_tool=suggested_tool,
                score=BUMP_AMOUNT,
                last_bumped_at=now,
                created_at=now,
            )
            db.add(row)
            await db.commit()
            return BUMP_AMOUNT
        new_score = min(SCORE_CAP, (row.score or 0) + BUMP_AMOUNT)
        row.score = new_score
        row.last_bumped_at = now
        await db.commit()
        return new_score
    except Exception as exc:
        logger.debug("caller_capability_score.bump failed: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass
        return 0


async def best_suggestion_for_key(
    db: AsyncSession,
    api_key_id: str,
) -> Optional[tuple[str, int]]:
    """Return ``(suggested_tool, score)`` for the highest-scoring tool
    that crosses the threshold for this caller, or ``None`` if no row
    qualifies. Used by the response middleware to decide whether to
    emit ``X-Proxy-MCP-Suggestion``.

    Picks the single highest score deterministically (max score, then
    alphabetic tool name as tiebreaker) — emitting multiple
    suggestions per response would just add header pollution and the
    accept handler (Ship 3) only flips one tool at a time anyway.
    """
    if not api_key_id:
        return None
    try:
        threshold = await _threshold()
        rs = await db.execute(
            select(CallerCapabilityScore.suggested_tool, CallerCapabilityScore.score)
            .where(CallerCapabilityScore.api_key_id == api_key_id)
            .where(CallerCapabilityScore.score >= threshold)
            .order_by(CallerCapabilityScore.score.desc(), CallerCapabilityScore.suggested_tool.asc())
            .limit(1)
        )
        row = rs.first()
        if row is None:
            return None
        return (row[0], int(row[1]))
    except Exception as exc:
        logger.debug("caller_capability_score.best_for_key failed: %s", exc)
        return None


async def decay_all_scores(db: AsyncSession) -> tuple[int, int]:
    """Multiply every score by ~0.96 (0.85^0.25 → ~24h half-life when
    fired every 6h). Returns ``(updated_count, gc_count)``.

    GC: rows with score below GC_BELOW are dropped — a single-event
    bump that was never reinforced disappears after ~6 ticks (~36h).
    """
    try:
        from app.models.db import CallerCapabilityScore as Row
        # 0.85^(6/24) = 0.85^0.25 ≈ 0.9605. Stored as ×100 ints, so
        # the math is: new = floor(old * 96 / 100). One UPDATE, no
        # per-row Python.
        result = await db.execute(
            update(Row).values(score=(Row.score * 96) / 100)
        )
        await db.commit()
        updated = result.rowcount if result.rowcount is not None else 0

        # GC. Second UPDATE because SQLite doesn't allow DELETE-with-UPDATE
        # in one statement reliably across versions.
        from sqlalchemy import delete
        gc = await db.execute(delete(Row).where(Row.score < GC_BELOW))
        await db.commit()
        gc_count = gc.rowcount if gc.rowcount is not None else 0
        return (updated, gc_count)
    except Exception as exc:
        logger.warning("caller_capability_score.decay failed: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass
        return (0, 0)
