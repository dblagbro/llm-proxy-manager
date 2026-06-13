"""v5.4.0 — Admin diagnostic endpoint for the AI provider supervisor.

Closes BUG-070 (post-refactor sweep 2026-06-12). Discovered on
tmrwww01: ``ai_provider_supervisor_enabled = True`` but zero
``supervisor_*`` activity events in 7 days. Three hypotheses
(silent crash / interval defaulting / event-write broken) cannot be
disambiguated from a snapshot probe. This endpoint forces one tick
synchronously and returns the full result so an operator can see
exactly what the supervisor would do.

The endpoint is admin-gated. It does NOT respect the
``ai_provider_supervisor_enabled`` flag — the whole point is to
diagnose a worker that may not be running.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db

router = APIRouter(prefix="/api/admin/ai-supervisor", tags=["admin"])


@router.post("/run-once")
async def supervisor_run_once(
    db: AsyncSession = Depends(get_db),
    _admin: AdminUser = Depends(require_admin),
):
    """Force one supervisor tick. See module docstring."""
    return await _supervisor_run_once_impl(db, _admin)


@router.post("/retry-tap-self-test")
async def retry_tap_self_test(
    _db: AsyncSession = Depends(get_db),
    _admin: AdminUser = Depends(require_admin),
):
    """v5.4.0 (BUG-072): exercise the openai-python retry tap end-to-end.

    Emits a synthetic record matching the retry log pattern and reports
    whether the tap captured it. Combined with ``/health.workers`` after
    the next sweep, gives the operator two ways to verify the v5.3.4
    instrumentation is alive.
    """
    from app.observability.openai_retry_tap import self_test
    return self_test()


async def _supervisor_run_once_impl(db, _admin):
    """Force one supervisor tick. Returns counts + any captured errors.

    Bypasses the ``ai_provider_supervisor_enabled`` flag so that an
    operator can probe even when the worker is disabled. Does NOT
    bypass ``auto_apply`` — verdicts are still recorded as suggestions
    unless the system flag says otherwise.
    """
    from app.monitoring.ai_provider_supervisor import _scan_all_once
    try:
        counts = await _scan_all_once()
        return {
            "ok": True,
            "counts": counts,
            "reviewed": counts.get("reviewed", 0),
            "skipped_locked": counts.get("skipped_locked", 0),
            "skipped_no_traffic": counts.get("skipped_no_traffic", 0),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": repr(exc),
            "error_type": type(exc).__name__,
        }
