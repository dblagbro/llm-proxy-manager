"""API key management endpoints."""
import secrets
import time
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from app.models.database import get_db
from app.models.db import ApiKey, Provider, ModelCapability
from app.auth.admin import require_admin, AdminUser
from app.auth.keys import generate_api_key
from app.auth.key_encryption import encrypt_key, decrypt_key
from app.auth.rate_limit_tiers import get_tier, list_tiers, tier_names
from app.utils.timefmt import utc_iso
from app.compliance import (
    KNOWN_COMPANIES,
    emit_policy_change,
    invalidate_blocklist_cache,
)
from app.compliance.policy import get_custom_companies

router = APIRouter(prefix="/api/keys", tags=["api-keys"])


class KeyCreate(BaseModel):
    name: str
    key_type: str = "standard"  # standard | claude-code | admin | admin-readonly-catalog
    # BUG-041 fix: reject negative numeric values at the boundary. The
    # KeyUpdate (PATCH) path treats negatives as "clear the limit/cap"
    # (documented sentinel); creation has no such semantic — a brand-new
    # key has nothing to clear.
    spending_cap_usd: Optional[float] = Field(default=None, ge=0)
    rate_limit_rpm: Optional[int] = Field(default=None, ge=0)
    rate_limit_tier: Optional[str] = None  # Wave 6: named tier
    daily_soft_cap_usd: Optional[float] = Field(default=None, ge=0)
    daily_hard_cap_usd: Optional[float] = Field(default=None, ge=0)
    hourly_cap_usd: Optional[float] = Field(default=None, ge=0)
    semantic_cache_enabled: bool = False
    # v5.0.0 — compliance policy fields. Validation occurs in
    # ``_validate_blocked_companies``; reason is required only on POST when
    # blocked_companies/allowed_paths are non-empty (policy edit).
    blocked_companies: Optional[List[str]] = None
    allowed_paths: Optional[List[str]] = None
    # v5.2.1 / Batch V2 — fine-grained policy fields. Non-empty
    # ``allowed_companies`` switches that dimension to allowlist mode.
    # ``*_models`` entries can be exact model names or fnmatch globs
    # ("claude-*", "gpt-4-*-turbo"); matched against the provider's
    # default_model AND the request's requested_model. See
    # ``docs/vendor-neutrality.md`` for semantics + examples.
    allowed_companies: Optional[List[str]] = None
    blocked_models: Optional[List[str]] = None
    allowed_models: Optional[List[str]] = None
    debug_echo_enabled: Optional[bool] = False
    reason: Optional[str] = None
    # v5.1.0 / Batch B2 — copy caps + compliance fields from an existing
    # key. When ``copy_from_id`` is set, ANY of the cap/compliance
    # fields above that are left at their default (None / False) are
    # populated from the source key. ``name`` + ``key_type`` are
    # always taken from THIS body, never the source.
    copy_from_id: Optional[str] = None


class KeyUpdate(BaseModel):
    name: Optional[str] = None
    key_type: Optional[str] = None
    enabled: Optional[bool] = None
    spending_cap_usd: Optional[float] = None  # -1 to clear the cap
    rate_limit_rpm: Optional[int] = None       # -1 to clear the limit
    rate_limit_tier: Optional[str] = None      # "" to clear
    daily_soft_cap_usd: Optional[float] = None   # -1 to clear
    daily_hard_cap_usd: Optional[float] = None   # -1 to clear
    hourly_cap_usd: Optional[float] = None       # -1 to clear
    semantic_cache_enabled: Optional[bool] = None
    # v3.9.13 — per-key caller_memory retention. None = no change;
    # 0 or negative = clear (no TTL); positive int = days until
    # background sweeper tombstones unused rows.
    caller_memory_ttl_days: Optional[int] = None
    # v5.0.0 — compliance policy edits. Decision 6 — ``reason`` is mandatory
    # when either ``blocked_companies`` or ``allowed_paths`` change.
    blocked_companies: Optional[List[str]] = None
    allowed_paths: Optional[List[str]] = None
    # v5.2.1 — fine-grained policy fields. ``reason`` is mandatory on
    # change for these too (audit-trail consistency with blocked_companies).
    allowed_companies: Optional[List[str]] = None
    blocked_models: Optional[List[str]] = None
    allowed_models: Optional[List[str]] = None
    debug_echo_enabled: Optional[bool] = None
    # v5.20.7 — refusal detection + cascade toggles surfaced on the
    # PATCH surface. These are NOT compliance-policy fields (no reason
    # required); they're per-key debug/behavior flags that DevinGPT
    # and others toggle at will. Values are pass-through: True/False
    # sets the flag; None means "don't touch."
    refusal_detection_enabled: Optional[bool] = None
    refusal_prompt_hardening: Optional[bool] = None
    refusal_retry_enabled: Optional[bool] = None
    refusal_retry_max_attempts: Optional[int] = None
    # v5.20.10 — self-edit permissions list (JSON column). Set to a
    # list of field names to grant the caller's AI self-update rights.
    # Empty list = revoke all (self-edit disabled). NULL/None (via
    # omission in the PATCH) leaves the existing setting unchanged.
    self_edit_permissions: Optional[List[str]] = None
    reason: Optional[str] = None


@router.get("")
async def list_keys(
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    # v3.0.20: hide tombstoned rows from the admin list — kept in DB only
    # for cluster-sync to propagate the delete to peers.
    # v5.1.0 / Batch B1: pass ?include_deleted=true to surface the trash
    # bin (admin Trash tab). Tombstoned rows show ``deleted_at`` so the
    # UI can compute the remaining restore window relative to
    # ``api_key_tombstone_retention_days``.
    q = select(ApiKey).order_by(ApiKey.created_at.desc())
    if not include_deleted:
        q = q.where(ApiKey.deleted_at.is_(None))
    result = await db.execute(q)
    keys = result.scalars().all()
    return [_serialize(k) for k in keys]


@router.post("/{key_id}/restore")
async def restore_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v5.1.0 / Batch B1 — restore a tombstoned key by clearing
    ``deleted_at`` + re-enabling. Returns 404 if the key was never
    tombstoned OR if it has passed the retention window.
    """
    from datetime import datetime, timezone, timedelta
    from app.config import settings as _s
    rs = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    k = rs.scalar_one_or_none()
    if k is None:
        raise HTTPException(404, "key not found")
    if k.deleted_at is None:
        raise HTTPException(400, "key is not deleted; nothing to restore")
    # Honor the retention window — past it, the key is considered
    # purged-pending-prune and cannot be restored.
    retention_days = int(getattr(_s, "api_key_tombstone_retention_days", 90) or 90)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    dt = k.deleted_at if k.deleted_at.tzinfo else k.deleted_at.replace(tzinfo=timezone.utc)
    if dt < cutoff:
        raise HTTPException(
            410,
            f"key was deleted more than {retention_days} days ago "
            f"(retention window expired). The next prune sweep will "
            f"hard-delete it.",
        )
    k.deleted_at = None
    k.enabled = True
    import time as _t
    k.last_user_edit_at = _t.time()
    await db.commit()
    return {"ok": True, "id": k.id, "restored_at": _t.time()}


@router.post("")
async def create_key(
    body: KeyCreate,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    raw_key, key_hash = generate_api_key()
    # v5.1.0 / Batch B2 — copy-from-existing. Populate any unset
    # cap/compliance fields from the source key. Operator UX: pick a
    # key from the dropdown, type a new name, hit Create — no need to
    # re-enter all the caps and policy by hand.
    if body.copy_from_id:
        src_rs = await db.execute(
            select(ApiKey).where(ApiKey.id == body.copy_from_id,
                                 ApiKey.deleted_at.is_(None))
        )
        src = src_rs.scalar_one_or_none()
        if src is None:
            raise HTTPException(404, f"copy_from_id key {body.copy_from_id} not found")
        if body.spending_cap_usd is None:
            body.spending_cap_usd = src.spending_cap_usd
        if body.rate_limit_rpm is None:
            body.rate_limit_rpm = src.rate_limit_rpm
        if body.rate_limit_tier is None:
            body.rate_limit_tier = src.rate_limit_tier
        if body.daily_soft_cap_usd is None:
            body.daily_soft_cap_usd = src.daily_soft_cap_usd
        if body.daily_hard_cap_usd is None:
            body.daily_hard_cap_usd = src.daily_hard_cap_usd
        if body.hourly_cap_usd is None:
            body.hourly_cap_usd = src.hourly_cap_usd
        if body.semantic_cache_enabled is False:
            body.semantic_cache_enabled = bool(src.semantic_cache_enabled)
        if body.blocked_companies is None:
            body.blocked_companies = list(src.blocked_companies) if src.blocked_companies else None
        if body.allowed_paths is None:
            body.allowed_paths = list(src.allowed_paths) if src.allowed_paths else None
        # v5.2.1 — copy fine-grained policy too. Same null-means-"take-source"
        # semantics so an operator cloning a strict-policy key gets the
        # same allowlist + per-model rules by default.
        if body.allowed_companies is None and getattr(src, "allowed_companies", None):
            body.allowed_companies = list(src.allowed_companies)
        if body.blocked_models is None and getattr(src, "blocked_models", None):
            body.blocked_models = list(src.blocked_models)
        if body.allowed_models is None and getattr(src, "allowed_models", None):
            body.allowed_models = list(src.allowed_models)
        if body.debug_echo_enabled is False:
            body.debug_echo_enabled = bool(src.debug_echo_enabled)
        # Reason is auto-set if copied compliance fields and the
        # operator didn't provide one explicitly.
        if (
            body.blocked_companies or body.allowed_paths
            or body.allowed_companies or body.blocked_models or body.allowed_models
        ) and not body.reason:
            body.reason = f"copied from key {src.key_prefix} ({src.name})"

    # v5.0.0 — validate compliance fields. ``blocked_companies`` must be
    # known company IDs; reason required when policy is set at create time.
    # v5.2.1 — extended to allowed_companies + per-model patterns. Same
    # reason-on-policy-set rule applies.
    if body.blocked_companies:
        _validate_blocked_companies(body.blocked_companies)
    if body.allowed_companies:
        _validate_blocked_companies(body.allowed_companies)
    if body.blocked_models:
        _validate_model_patterns(body.blocked_models)
    if body.allowed_models:
        _validate_model_patterns(body.allowed_models)
    _v521_set = any((
        body.allowed_companies, body.blocked_models, body.allowed_models,
    ))
    if (body.blocked_companies or body.allowed_paths or _v521_set) and not (
        body.reason and body.reason.strip()
    ):
        raise HTTPException(422, "reason required when setting compliance policy")
    key = ApiKey(
        id=secrets.token_hex(8),
        name=body.name,
        key_hash=key_hash,
        key_prefix=raw_key[:12],
        encrypted_key=encrypt_key(raw_key),  # admin-reveal requires this
        key_type=body.key_type,
        enabled=True,
        spending_cap_usd=body.spending_cap_usd,
        rate_limit_rpm=body.rate_limit_rpm,
        rate_limit_tier=_validate_tier(body.rate_limit_tier),
        daily_soft_cap_usd=body.daily_soft_cap_usd,
        daily_hard_cap_usd=body.daily_hard_cap_usd,
        hourly_cap_usd=body.hourly_cap_usd,
        semantic_cache_enabled=body.semantic_cache_enabled,
        blocked_companies=body.blocked_companies,
        allowed_paths=body.allowed_paths,
        # v5.2.1 — fine-grained policy fields persisted on create
        allowed_companies=body.allowed_companies,
        blocked_models=body.blocked_models,
        allowed_models=body.allowed_models,
        debug_echo_enabled=bool(body.debug_echo_enabled),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    if body.blocked_companies or body.allowed_paths or _v521_set:
        await _emit_compliance_policy_change(
            db, key=key,
            before={
                "blocked_companies": None, "allowed_paths": None,
                "allowed_companies": None,
                "blocked_models": None, "allowed_models": None,
            },
            after={
                "blocked_companies": body.blocked_companies,
                "allowed_paths":     body.allowed_paths,
                "allowed_companies": body.allowed_companies,
                "blocked_models":    body.blocked_models,
                "allowed_models":    body.allowed_models,
            },
            reason=body.reason or "initial-policy-on-create",
            user_id=user.user_id,
        )
        invalidate_blocklist_cache(key.id)
        await _push_compliance_sync()
    # Return raw key ONCE — never stored, never retrievable again
    result = _serialize(key)
    result["raw_key"] = raw_key
    return result


@router.patch("/{key_id}")
async def update_key(
    key_id: str,
    body: KeyUpdate,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    k = await _get_or_404(db, key_id)
    if body.name is not None:
        k.name = body.name
    if body.key_type is not None:
        k.key_type = body.key_type
    if body.enabled is not None:
        k.enabled = body.enabled
    if body.spending_cap_usd is not None:
        k.spending_cap_usd = None if body.spending_cap_usd < 0 else body.spending_cap_usd
    if body.rate_limit_rpm is not None:
        k.rate_limit_rpm = None if body.rate_limit_rpm < 0 else body.rate_limit_rpm
    if body.rate_limit_tier is not None:
        k.rate_limit_tier = _validate_tier(body.rate_limit_tier) if body.rate_limit_tier else None
    if body.daily_soft_cap_usd is not None:
        k.daily_soft_cap_usd = None if body.daily_soft_cap_usd < 0 else body.daily_soft_cap_usd
    if body.daily_hard_cap_usd is not None:
        k.daily_hard_cap_usd = None if body.daily_hard_cap_usd < 0 else body.daily_hard_cap_usd
    if body.hourly_cap_usd is not None:
        k.hourly_cap_usd = None if body.hourly_cap_usd < 0 else body.hourly_cap_usd
    if body.semantic_cache_enabled is not None:
        k.semantic_cache_enabled = body.semantic_cache_enabled
    if body.caller_memory_ttl_days is not None:
        # 0 or negative clears the TTL; positive int sets it
        k.caller_memory_ttl_days = (
            None if body.caller_memory_ttl_days <= 0
            else int(body.caller_memory_ttl_days)
        )
    # v5.0.0 — compliance fields. Detect a real change and require ``reason``
    # (decision 6). Snapshot before-state for the audit row + invalidate the
    # in-process blocklist cache + trigger a quorum sync push.
    policy_changed = False
    before = {
        "blocked_companies": list(k.blocked_companies) if k.blocked_companies else None,
        "allowed_paths":     list(k.allowed_paths)     if k.allowed_paths     else None,
        # v5.2.1 — fine-grained policy snapshot in the audit row
        "allowed_companies": list(k.allowed_companies) if k.allowed_companies else None,
        "blocked_models":    list(k.blocked_models)    if k.blocked_models    else None,
        "allowed_models":    list(k.allowed_models)    if k.allowed_models    else None,
    }
    if body.blocked_companies is not None:
        _validate_blocked_companies(body.blocked_companies)
        if (k.blocked_companies or None) != (body.blocked_companies or None):
            k.blocked_companies = body.blocked_companies or None
            policy_changed = True
    if body.allowed_paths is not None:
        if (k.allowed_paths or None) != (body.allowed_paths or None):
            k.allowed_paths = body.allowed_paths or None
            policy_changed = True
    # v5.2.1 — fine-grained policy update wiring. Allowed_companies
    # reuses the company-id validator (same constraint applies). Model
    # patterns are accepted as strings — the policy engine evaluates
    # them via fnmatch at match time, so the only thing to validate is
    # that they're non-empty strings.
    if body.allowed_companies is not None:
        _validate_blocked_companies(body.allowed_companies)
        if (k.allowed_companies or None) != (body.allowed_companies or None):
            k.allowed_companies = body.allowed_companies or None
            policy_changed = True
    if body.blocked_models is not None:
        _validate_model_patterns(body.blocked_models)
        if (k.blocked_models or None) != (body.blocked_models or None):
            k.blocked_models = body.blocked_models or None
            policy_changed = True
    if body.allowed_models is not None:
        _validate_model_patterns(body.allowed_models)
        if (k.allowed_models or None) != (body.allowed_models or None):
            k.allowed_models = body.allowed_models or None
            policy_changed = True
    if body.debug_echo_enabled is not None:
        k.debug_echo_enabled = bool(body.debug_echo_enabled)
    # v5.20.7 — refusal fields. Not policy-gated (no reason required).
    if body.refusal_detection_enabled is not None:
        k.refusal_detection_enabled = bool(body.refusal_detection_enabled)
    if body.refusal_prompt_hardening is not None:
        k.refusal_prompt_hardening = bool(body.refusal_prompt_hardening)
    if body.refusal_retry_enabled is not None:
        k.refusal_retry_enabled = bool(body.refusal_retry_enabled)
    if body.refusal_retry_max_attempts is not None:
        # Sentinel -1 = clear back to NULL (worker default of 3 applies).
        val = int(body.refusal_retry_max_attempts)
        k.refusal_retry_max_attempts = None if val < 0 else val
    # v5.20.10 — self_edit_permissions JSON list. None (not in payload)
    # = untouched; [] = revoke all; ["field","field"] = grant listed.
    if body.self_edit_permissions is not None:
        # Filter to ELIGIBLE_FIELDS server-side so a stale frontend can't
        # accidentally send a never-editable field into the DB.
        try:
            from app.integration.self_update import ELIGIBLE_FIELDS
            filtered = [
                f for f in body.self_edit_permissions
                if f in ELIGIBLE_FIELDS
            ]
        except Exception:
            filtered = list(body.self_edit_permissions or [])
        k.self_edit_permissions = filtered or None
    if policy_changed and not (body.reason and body.reason.strip()):
        raise HTTPException(422, "reason required for compliance policy edits")
    # v4.4.20 — stamp the LWW gate. Mirror of provider PATCH: only
    # operator-initiated edits bump this; cost-bucket / last_used_at
    # writes from request hot-paths do NOT, so background traffic
    # can't ping-pong a real edit on a peer.
    k.last_user_edit_at = time.time()
    await db.commit()
    if policy_changed:
        after = {
            "blocked_companies": list(k.blocked_companies) if k.blocked_companies else None,
            "allowed_paths":     list(k.allowed_paths)     if k.allowed_paths     else None,
            "allowed_companies": list(k.allowed_companies) if k.allowed_companies else None,
            "blocked_models":    list(k.blocked_models)    if k.blocked_models    else None,
            "allowed_models":    list(k.allowed_models)    if k.allowed_models    else None,
        }
        await _emit_compliance_policy_change(
            db, key=k, before=before, after=after,
            reason=body.reason, user_id=user.user_id,
        )
        invalidate_blocklist_cache(k.id)
        await _push_compliance_sync()
    return _serialize(k)


@router.get("/{key_id}/reveal")
async def reveal_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Return the decrypted raw key. Admin-only.

    Returns 404 for legacy keys created before encryption-at-rest support.
    """
    k = await _get_or_404(db, key_id)
    raw = decrypt_key(k.encrypted_key)
    if raw is None:
        raise HTTPException(404, "Raw key not retrievable (legacy pre-encryption key — delete and recreate)")
    return {"id": k.id, "raw_key": raw}


class BulkDeleteBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/bulk-delete")
async def bulk_delete_keys(
    body: BulkDeleteBody,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Delete multiple API keys in one call. Returns count deleted.

    v3.0.20: soft-delete via tombstone (``deleted_at`` + ``enabled=False``).
    Hard DELETE was reversed by the next cluster-sync push from a peer that
    still had the row — same shape as the v2.8.2 Provider resurrection bug.
    """
    if not body.ids:
        return {"deleted": 0}
    result = await db.execute(
        select(ApiKey).where(ApiKey.id.in_(body.ids), ApiKey.deleted_at.is_(None))
    )
    keys = result.scalars().all()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    count = 0
    for k in keys:
        k.deleted_at = now
        k.enabled = False
        count += 1
    await db.commit()
    return {"deleted": count, "requested": len(body.ids)}


@router.delete("/{key_id}")
async def delete_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.0.20: soft-delete via tombstone — see bulk_delete_keys for context."""
    from datetime import datetime, timezone
    k = await _get_or_404(db, key_id)
    k.deleted_at = datetime.now(timezone.utc)
    k.enabled = False
    await db.commit()
    return {"ok": True}


@router.post("/_purge-test-tombstones")
async def purge_test_tombstones(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.1.x: hard-delete tombstoned api_keys whose name matches a test
    pattern AND whose ``deleted_at`` is older than 60s (allows cluster
    sync to converge so peers don't resurrect the rows).

    Used by integration test ``pytest_sessionfinish`` hooks to prevent
    tombstone bloat — without this, every test session leaves rows that
    sit in cluster_sync payload for the full 7-day tombstone retention
    window, eventually slowing apply_sync the same way the 2026-05-07
    incident did (127 stale tombstones → ~3s sync apply per cycle).

    Safe in production: only affects keys named ``pytest-…``,
    ``test-playwright-…``, ``cot-debug-…``, or ``debug-…``. Admin-gated.
    """
    from sqlalchemy import delete, or_, func
    from app.models.db import ApiKey

    cutoff = func.datetime("now", "-60 seconds")
    patterns = (
        "pytest-%",
        "pytest-cot-%",
        "test-playwright-%",
        "cot-debug-%",
        "debug-%",
    )
    rs = await db.execute(
        delete(ApiKey)
        .where(ApiKey.deleted_at.is_not(None))
        .where(ApiKey.deleted_at < cutoff)
        .where(or_(*[ApiKey.name.like(p) for p in patterns]))
    )
    await db.commit()
    return {"ok": True, "purged": rs.rowcount}


def _validate_tier(tier_name: Optional[str]) -> Optional[str]:
    """Return the normalized tier name, or raise 400 if unknown."""
    if not tier_name:
        return None
    t = get_tier(tier_name)
    if t is None:
        raise HTTPException(
            400,
            f"Unknown rate_limit_tier '{tier_name}'. Valid: {', '.join(tier_names())}",
        )
    return t.name


@router.get("/tiers", tags=["api-keys"])
async def list_rate_limit_tiers(_: AdminUser = Depends(require_admin)):
    """Return the available named rate-limit tiers (Wave 6)."""
    return [
        {
            "name": t.name,
            "rpm": t.rpm,
            "rpd": t.rpd,
            "burst": t.burst,
            "description": t.description,
        }
        for t in list_tiers()
    ]


@router.get("/{key_id}/models")
async def list_key_models(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.10.8 — the effective model catalog for one API key: every
    model offered by a provider the key is allowed to route to.

    A provider with ``Provider.owned_by_key_id`` set is private to that
    key (v3.0.45 tenant scoping); ``NULL`` = shared by all keys. So the
    key's effective providers are the shared ones plus the ones it
    owns, and its model list is the union of those providers'
    ``ModelCapability`` rows (+ each provider's ``default_model``).

    Powers the admin API-Keys page "Copy models" action.
    """
    key = await _get_or_404(db, key_id)
    providers = (await db.execute(
        select(Provider)
        .where(Provider.enabled == True)  # noqa: E712
        .where(Provider.deleted_at.is_(None))
        .where(or_(
            Provider.owned_by_key_id.is_(None),
            Provider.owned_by_key_id == key_id,
        ))
    )).scalars().all()

    models: set[str] = set()
    provider_ids = [p.id for p in providers]
    if provider_ids:
        caps = (await db.execute(
            select(ModelCapability.model_id)
            .where(ModelCapability.provider_id.in_(provider_ids))
            .where(ModelCapability.deleted_at.is_(None))
        )).scalars().all()
        models.update(m for m in caps if m)
    for p in providers:
        if p.default_model:
            models.add(p.default_model)

    return {
        "key_id": key.id,
        "key_name": key.name,
        "count": len(models),
        "models": sorted(models, key=str.lower),
    }


async def _get_or_404(db: AsyncSession, key_id: str) -> ApiKey:
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    k = result.scalar_one_or_none()
    if not k:
        raise HTTPException(404, "API key not found")
    return k


def _serialize(k: ApiKey) -> dict:
    # v5.9.5 — coalesce counter fields to 0. Pre-v5.9.5 these came back
    # as raw column values; a never-used key (e.g. just-created) or a
    # key written before the columns existed could land as NULL. The
    # frontend APIKeysPage renders ``k.total_requests.toLocaleString()``
    # with no null guard, so a single NULL row would throw
    # ``TypeError: Cannot read properties of null (reading 'toLocaleString')``
    # and the entire /keys page would render-error to a white screen.
    # The v580-integration-internal key hit this on 2026-06-22.
    return {
        "id": k.id,
        "name": k.name,
        "key_prefix": k.key_prefix,
        "key_type": k.key_type,
        "enabled": k.enabled,
        "total_requests": int(k.total_requests or 0),
        "total_tokens": int(k.total_tokens or 0),
        "total_cost_usd": float(k.total_cost_usd or 0.0),
        "spending_cap_usd": k.spending_cap_usd,
        "rate_limit_rpm": k.rate_limit_rpm,
        "rate_limit_tier": getattr(k, "rate_limit_tier", None),
        "daily_soft_cap_usd": k.daily_soft_cap_usd,
        "daily_hard_cap_usd": k.daily_hard_cap_usd,
        "hourly_cap_usd": k.hourly_cap_usd,
        "semantic_cache_enabled": bool(k.semantic_cache_enabled),
        "caller_memory_ttl_days": getattr(k, "caller_memory_ttl_days", None),
        # v5.0.0 — compliance per-key fields
        "blocked_companies": list(k.blocked_companies) if k.blocked_companies else None,
        "allowed_paths": list(k.allowed_paths) if k.allowed_paths else None,
        # v5.2.1 / Batch V2 — fine-grained vendor-neutrality policy
        "allowed_companies": list(getattr(k, "allowed_companies", None) or []) or None,
        "blocked_models":    list(getattr(k, "blocked_models",    None) or []) or None,
        "allowed_models":    list(getattr(k, "allowed_models",    None) or []) or None,
        "debug_echo_enabled": bool(getattr(k, "debug_echo_enabled", False)),
        # v5.20.7 — refusal detection + cascade flags for the admin UI
        "refusal_detection_enabled": bool(getattr(k, "refusal_detection_enabled", False)),
        "refusal_prompt_hardening":  bool(getattr(k, "refusal_prompt_hardening",  False)),
        "refusal_retry_enabled":     bool(getattr(k, "refusal_retry_enabled",     False)),
        "refusal_retry_max_attempts": getattr(k, "refusal_retry_max_attempts", None),
        # v5.20.10 — self_edit_permissions JSON list. null = self-edit
        # disabled (default); [] treated as disabled too (server-side
        # normalization).
        "self_edit_permissions": list(getattr(k, "self_edit_permissions", None) or []) or None,
        "day_cost_usd": float(k.day_cost_usd or 0.0),
        "hour_cost_usd": float(k.hour_cost_usd or 0.0),
        "can_reveal": bool(k.encrypted_key),
        "last_used_at": utc_iso(k.last_used_at),
        "created_at": utc_iso(k.created_at),
        # v5.1.1 / Batch B1 UI — Trash tab needs deleted_at to render
        # remaining-restore-window. Always emitted; null for live keys.
        "deleted_at": utc_iso(k.deleted_at),
    }


def _validate_blocked_companies(ids: List[str]) -> None:
    """Reject unknown company IDs. Allowed = KNOWN_COMPANIES keys ∪ custom-companies IDs."""
    if not ids:
        return
    allowed = set(KNOWN_COMPANIES.keys()) | set(get_custom_companies().keys())
    bad = [c for c in ids if c not in allowed]
    if bad:
        raise HTTPException(
            400,
            f"Unknown company IDs in blocked_companies: {bad}. "
            f"Allowed: {sorted(allowed)}",
        )


# v5.2.1 / Batch V2 — model patterns can be exact names ("claude-opus-4-0")
# or fnmatch globs ("claude-*", "gpt-4-*-turbo"). The policy engine
# evaluates them with fnmatch at match time, so the boundary check is
# narrow: non-empty strings, no whitespace, plausible length. Anything
# else falls through to the matcher which simply won't match.
_MAX_MODEL_PATTERN_LEN = 128


def _validate_model_patterns(patterns: List[str]) -> None:
    if not patterns:
        return
    bad = [
        p for p in patterns
        if not isinstance(p, str) or not p.strip()
        or len(p) > _MAX_MODEL_PATTERN_LEN
        or any(ch.isspace() for ch in p)
    ]
    if bad:
        raise HTTPException(
            400,
            f"Invalid model patterns: {bad}. Each must be a non-empty "
            f"string ≤{_MAX_MODEL_PATTERN_LEN} chars with no whitespace. "
            f"Wildcards (*, ?) are honored by the matcher.",
        )


async def _emit_compliance_policy_change(
    db, *, key: ApiKey, before: dict, after: dict, reason: str, user_id: Optional[str],
) -> None:
    """Wrap ``emit_policy_change`` + best-effort quorum fan-out. Records the
    fan-out result on the policy-change row. Falls back to ``applied=[]``,
    ``pending=[]`` when the cluster manager doesn't have the v5 helper."""
    applied_peers, pending_peers = [], []
    try:
        from app.cluster import manager as _cm
        if hasattr(_cm, "push_policy_change_with_quorum"):
            n_peers = len(getattr(_cm, "peers", {}) or {})
            required = max(0, n_peers - 1)
            result = await _cm.push_policy_change_with_quorum(
                {"scope": "per_key", "target_id": key.id, "after": after},
                required_acks=required,
            )
            applied_peers = result.get("applied_to_peers", [])
            pending_peers = result.get("pending_peers", [])
    except Exception:
        pending_peers = [{"peer": "unknown", "reason": "quorum-push-failed"}]
    await emit_policy_change(
        db, scope="per_key", target_id=key.id,
        before=before, after=after, reason=reason,
        changed_by_user_id=user_id,
        applied_to_peers=applied_peers, pending_peers=pending_peers,
        commit=True,
    )


async def _push_compliance_sync() -> None:
    """Trigger a regular cluster sync push so peers pick up the new key
    fields. ``push_policy_change_with_quorum`` covers the active-peer fan-out;
    this catches recovering peers via the normal sync loop."""
    try:
        from app.config import settings as _s
        if not _s.cluster_enabled:
            return
        import asyncio
        from app.cluster.manager import peers as cluster_peers, push_sync
        from app.models.database import AsyncSessionLocal
        for peer in list(cluster_peers.values()):
            if peer.status != "unreachable":
                asyncio.create_task(push_sync(peer, AsyncSessionLocal))
    except Exception:
        pass
