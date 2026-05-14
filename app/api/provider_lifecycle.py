"""v3.9.8 (P5 refactor) — provider lifecycle endpoints extracted from
``app/api/providers.py`` to keep that file under 1000 lines.

Endpoints:
- POST /api/providers/{id}/clear-auth-failure
- PATCH /api/providers/{id}/toggle
- POST /api/providers/_release-manual-overrides
- POST /api/providers/{id}/test
- POST /api/providers/{id}/scan-models

The router uses the same ``/api/providers`` prefix as providers.py;
registered separately in main.py. ``_get_or_404`` and ``_serialize``
are imported from providers.py to avoid duplication.
"""
import time

from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import Provider
from app.auth.admin import require_admin, AdminUser
from app.providers.scanner import scan_provider_models, test_provider
from app.monitoring.status import register_provider

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _stamp_user_edit(p: Provider) -> None:
    p.last_user_edit_at = time.time()


@router.post("/{provider_id}/clear-auth-failure")
async def clear_provider_auth_failure(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v2.7.8 BUG-002: clear the 'needs re-auth' flag for a provider.

    Called by the UI's "Mark Re-Authed" button, by save-with-new-key
    handlers, and by the OAuth rotate endpoint. Does NOT close the
    circuit breaker on its own — admin must hit Test for that, or the
    next successful call will close it via record_outcome.
    """
    from app.routing.circuit_breaker import clear_auth_failure
    await _get_or_404(db, provider_id)
    clear_auth_failure(provider_id)
    return {"ok": True}


@router.patch("/{provider_id}/toggle")
async def toggle_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """v3.7.28 (#252 phase 1): toggling now also sets/clears the manual
    override lock so the AI supervisor (when it ships) can't reverse
    the operator's explicit decision.

    - Disable → enabled=False AND manual_override_until=indefinite
    - Enable  → enabled=True  AND manual_override_until=NULL (released)

    The supervisor reads ``manual_override_until`` and skips any
    provider where it's non-null. Operator's UI banner surfaces the
    set of locked providers; "Release all" clears them in bulk via
    POST /api/providers/_release-manual-overrides.
    """
    from datetime import datetime as _dt
    INDEFINITE_LOCK = _dt(9999, 12, 31, 23, 59, 59)

    p = await _get_or_404(db, provider_id)
    new_state = not p.enabled
    p.enabled = new_state
    now = _dt.utcnow()
    if not new_state:
        # Disable click → set manual override (sticky against AI)
        p.manual_override_until = INDEFINITE_LOCK
        p.manual_override_set_by = getattr(user, "id", None) or getattr(user, "username", None)
        p.manual_override_set_at = now
    else:
        # Enable click → release any prior manual override
        p.manual_override_until = None
        p.manual_override_set_by = None
        p.manual_override_set_at = None
        p.manual_override_reason = None
    _stamp_user_edit(p)
    await db.commit()
    return {
        "enabled": p.enabled,
        "manual_override_active": p.manual_override_until is not None,
    }


@router.post("/_release-manual-overrides")
async def release_manual_overrides(
    enable: bool = True,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.7.28 (#252 phase 1): bulk-clear manual override on all
    providers — the banner's "Release & re-enable all" button.

    v3.8.6 behavior change: by default, ALSO sets enabled=True on the
    affected rows. Pre-v3.8.6 the endpoint left enabled unchanged,
    which produced an operator-confusing UX: clicking "Release" left
    providers disabled, the provider detail then showed an "Enable"
    button, and the operator was stuck wondering whether their click
    had any effect.

    Why this is safe: the only way a provider got into
    ``manual_override_until=non-null AND enabled=False`` is via the
    operator clicking Disable. Releasing the lock and re-enabling
    is the inverse of that single user action — the natural symmetric
    "Release & re-enable" the banner UX implies.

    Caller can override with ``?enable=false`` for explicit release-only
    behavior (e.g. operator script that wants to hand control to the
    AI supervisor without immediately re-enabling).
    """
    from sqlalchemy import update
    from app.models.db import Provider
    values = {
        "manual_override_until": None,
        "manual_override_set_by": None,
        "manual_override_set_at": None,
        "manual_override_reason": None,
    }
    if enable:
        values["enabled"] = True
    result = await db.execute(
        update(Provider)
        .where(Provider.manual_override_until.is_not(None))
        .where(Provider.deleted_at.is_(None))
        .values(**values)
    )
    await db.commit()
    return {"released": result.rowcount, "re_enabled": enable}


@router.post("/{provider_id}/test")
async def test_provider_endpoint(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    result = await test_provider(p)
    # v3.0.9: surface model-deprecation warning so operators see the
    # actionable fix BEFORE the upstream 404s on real traffic.
    from app.providers.deprecations import check_model_deprecation
    replacement = check_model_deprecation(p.default_model)
    if replacement:
        result = dict(result)
        result["deprecation_warning"] = (
            f"Provider's default_model {p.default_model!r} is deprecated by "
            f"the upstream vendor. Recommended replacement: {replacement!r}. "
            f"Update via Edit Provider or wait for the next startup migration."
        )
        result["recommended_default_model"] = replacement
    # v3.0.97 — log admin-action so operators have an audit trail.
    # Was previously invisible: no activity_log entry on test/scan/etc.
    try:
        from app.monitoring.activity import log_event
        ok = bool(result.get("ok", True))
        await log_event(
            db,
            event_type="provider_test",
            message=f"{p.name} · test {'ok' if ok else 'failed'}",
            severity="info" if ok else "warning",
            provider_id=p.id,
            metadata={
                "provider_name": p.name,
                "provider_type": p.provider_type,
                "ok": ok,
                "result_summary": {k: v for k, v in result.items()
                                   if k in ("ok", "error", "model", "latency_ms",
                                            "deprecation_warning",
                                            "recommended_default_model")},
            },
        )
    except Exception:
        pass  # never let logging failure break the response
    return result


@router.post("/{provider_id}/scan-models")
async def scan_models(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    try:
        models = await scan_provider_models(db, p)
        # v3.0.9: also flag deprecated models in the scan result so the
        # UI can render them with a warning + suggested replacement.
        # v3.0.16 fix: scan_provider_models returns list[dict] (each entry
        # has ``model_id``), not list[str] — the original comprehension
        # was treating each dict as a key, which raised "unhashable type:
        # 'dict'" the first time a non-empty scan landed.
        from app.providers.deprecations import MODEL_DEPRECATIONS
        deprecated_models = [
            {"id": m["model_id"], "replacement": MODEL_DEPRECATIONS[m["model_id"]]}
            for m in (models or [])
            if isinstance(m, dict) and m.get("model_id") in MODEL_DEPRECATIONS
        ]
        out = {"scanned": len(models), "models": models}
        if not models:
            out["warning"] = "No models discovered — check API key and provider type"
        if deprecated_models:
            out["deprecated_models"] = deprecated_models
        # v3.0.97 — log admin-action so operators have an audit trail.
        try:
            from app.monitoring.activity import log_event
            await log_event(
                db,
                event_type="provider_scan_models",
                message=f"{p.name} · scanned {len(models)} model{'s' if len(models) != 1 else ''}",
                severity="info" if models else "warning",
                provider_id=p.id,
                metadata={
                    "provider_name": p.name,
                    "provider_type": p.provider_type,
                    "scanned_count": len(models),
                    "model_ids": [m.get("model_id") for m in (models or [])
                                  if isinstance(m, dict)][:50],  # cap to keep meta lean
                    "deprecated_count": len(deprecated_models),
                },
            )
        except Exception:
            pass
        return out
    except Exception as e:
        # v3.0.97 — also log scan failures so operators see them.
        try:
            from app.monitoring.activity import log_event
            await log_event(
                db,
                event_type="provider_scan_models",
                message=f"{p.name} · scan failed",
                severity="error",
                provider_id=p.id,
                metadata={
                    "provider_name": p.name,
                    "provider_type": p.provider_type,
                    "error": str(e)[:500],
                    "error_class": "unknown",  # admin error class; v3.0.75 taxonomy is request-side
                },
            )
        except Exception:
            pass
        raise HTTPException(500, f"Model scan failed: {e}")




from app.api.providers import _get_or_404, _serialize  # noqa: E402
