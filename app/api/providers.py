"""Provider CRUD, test, model scan, and capability management."""
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.database import get_db
from app.models.db import Provider, ModelCapability
from app.auth.admin import require_admin, AdminUser
from app.providers.scanner import scan_provider_models, test_provider
from app.monitoring.status import register_provider
from app.routing.capability_inference import infer_capability_profile

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _stamp_user_edit(p: Provider) -> None:
    """v3.0.11: mark this row as having been touched by a real admin edit.
    Cluster sync prefers this timestamp over ``updated_at`` for LWW so that
    OAuth auto-refresh, deprecation auto-bump, or priority tie-breaks on
    a peer node can't revert a rename/config edit made on this node."""
    p.last_user_edit_at = time.time()


class ProviderCreate(BaseModel):
    name: str
    provider_type: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    priority: int = 10
    enabled: bool = True
    timeout_sec: int = 30
    exclude_from_tool_requests: bool = False
    hold_down_sec: Optional[int] = None       # None = use global setting
    failure_threshold: Optional[int] = None   # None = use global setting
    daily_budget_usd: Optional[float] = None  # None = unlimited
    extra_config: dict = {}
    # v2.7.0: claude-oauth credential paste. The frontend sends either:
    #   - bare ``sk-ant-oat...`` access token, OR
    #   - the JSON contents of ``~/.claude/credentials.json``.
    # Server parses, extracts access/refresh/expires_at, and stores them in
    # the existing api_key column + new oauth_* columns. The raw blob is
    # never persisted.
    oauth_credentials_blob: Optional[str] = None


class ProviderUpdate(ProviderCreate):
    pass


class CapabilityUpdate(BaseModel):
    tasks: list[str]
    latency: str
    cost_tier: str
    safety: int
    context_length: int
    regions: list[str]
    modalities: list[str]
    native_reasoning: bool
    native_tools: bool = True
    native_vision: bool = True


@router.get("")
async def list_providers(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    # v2.8.2: hide soft-deleted (tombstoned) providers from the UI.
    result = await db.execute(
        select(Provider)
        .where(Provider.deleted_at.is_(None))
        .order_by(Provider.priority)
    )
    providers = result.scalars().all()
    return [_serialize(p) for p in providers]


_TYPES_REQUIRING_API_KEY = {
    "anthropic", "openai", "google", "vertex", "grok",
    "cohere", "mistral", "groq", "together", "fireworks",
}


async def normalize_priority_ties(db: AsyncSession) -> int:
    """v2.8.2: one-shot sweep that resolves any existing ties by bumping
    the younger duplicates +1 in created_at order. Idempotent — runs at
    startup. Returns count of providers bumped (0 means already normalized).

    Example before: [a@1, b@2, c@2, d@3, e@3] (b created before c, d before e)
    After: [a@1, b@2, c@3, d@4, e@5] — younger row at each tie shifts up,
    and the cascade from c=3 collides with d=3, so d→4, then d=4 collides
    with previously-shifted nothing → no further cascade. The net effect is
    a strict total order by (priority, created_at).
    """
    from sqlalchemy import select as _select
    rows = (await db.execute(
        _select(Provider).order_by(Provider.priority.asc(), Provider.created_at.asc(), Provider.id.asc())
    )).scalars().all()
    bumped = 0
    seen_priorities: set[int] = set()
    for row in rows:
        if row.priority not in seen_priorities:
            seen_priorities.add(row.priority)
            continue
        # Tie — find the next free slot at or above row.priority
        new_pri = row.priority
        while new_pri in seen_priorities:
            new_pri += 1
        row.priority = new_pri
        seen_priorities.add(new_pri)
        bumped += 1
    if bumped:
        await db.flush()
    return bumped


async def _bump_priority_conflicts(
    db: AsyncSession,
    target_priority: int,
    *,
    exclude_id: Optional[str] = None,
) -> int:
    """v2.8.2: when a provider takes priority P, bump every other provider
    already at P (and any chain-reaction conflicts) by +1 so the new/updated
    row gets the slot it asked for.

    Example: providers at 1,2,3,4,5,6. New provider asks for 2 →
    existing-2 → 3, existing-3 → 4, existing-4 → 5, existing-5 → 6,
    existing-6 → 7. Final order: 1, NEW@2, 3, 4, 5, 6, 7.

    ``exclude_id`` is the row that's TAKING the slot — exclude it from the
    conflict lookup so we don't bump our own row in the create-then-bump
    or PUT flow.

    Returns the number of rows bumped (for logging / response telemetry).
    """
    bumped = 0
    # Snapshot all candidates upfront so the iteration doesn't re-query inside
    # an open transaction (avoids autoflush quirks with in-memory SQLite).
    snap = (await db.execute(
        select(Provider).where(
            (Provider.id != exclude_id) if exclude_id is not None else (Provider.id != "")
        ).order_by(Provider.priority.asc(), Provider.created_at.asc(), Provider.id.asc())
    )).scalars().all()

    # Group by ORIGINAL priority — chain-reactions only fire when an original
    # row sits at the next priority. Already-bumped rows don't re-bump.
    by_priority: dict[int, list] = {}
    for row in snap:
        by_priority.setdefault(row.priority, []).append(row)

    current_priority = target_priority
    while current_priority in by_priority:
        for row in by_priority[current_priority]:
            row.priority = current_priority + 1
            bumped += 1
        # Done with this bucket — don't re-process the bumped rows on the
        # next iteration. Chain-reaction continues only if ORIGINAL rows
        # already sat at current_priority+1.
        del by_priority[current_priority]
        current_priority += 1
    return bumped


@router.post("")
async def create_provider(
    body: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    data = body.model_dump()
    blob = data.pop("oauth_credentials_blob", None)

    # v2.7.6 BUG-019: reject providers that require auth but have no key.
    # Without this, the provider is enabled but every routed request 502s.
    if body.provider_type in _TYPES_REQUIRING_API_KEY and not (data.get("api_key") or "").strip():
        raise HTTPException(
            400,
            f"{body.provider_type} providers require an api_key — paste a key in the form.",
        )

    # v3.0.12: prevent duplicate-name providers (cluster-sync history made
    # this easy to do by accident). Boot-time dedup migration cleans up
    # legacy dups; this guard prevents new ones.
    from app.providers.dedup import name_is_taken
    if await name_is_taken(db, body.name):
        raise HTTPException(
            409,
            f"A provider named {body.name!r} already exists. "
            "Pick a unique name (or rename / delete the existing one first).",
        )

    if body.provider_type == "claude-oauth":
        if not blob:
            raise HTTPException(
                400,
                "claude-oauth providers require 'oauth_credentials_blob' — paste your "
                "`~/.claude/credentials.json` contents or a bare 'sk-ant-oat...' token.",
            )
        from app.providers.claude_oauth import parse_credentials, CredentialParseError
        try:
            creds = parse_credentials(blob)
        except CredentialParseError as e:
            raise HTTPException(400, f"Credential parse failed: {e}")
        data["api_key"] = creds.access_token
        data["oauth_refresh_token"] = creds.refresh_token
        data["oauth_expires_at"] = creds.expires_at
        if not data.get("default_model"):
            data["default_model"] = "claude-sonnet-4-6"
    elif blob:
        raise HTTPException(
            400,
            f"oauth_credentials_blob is only valid when provider_type='claude-oauth' "
            f"(got {body.provider_type!r})",
        )

    # v2.8.2: bump any existing provider already at this priority +1 (chained)
    # BEFORE inserting so the new row gets the requested slot cleanly.
    await _bump_priority_conflicts(db, data["priority"])

    provider = Provider(id=secrets.token_hex(8), **data)
    _stamp_user_edit(provider)
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    register_provider(provider.id, provider.provider_type, provider.hold_down_sec, provider.failure_threshold)
    # New provider — clear any stale auth-failure flag carried by id collision (defensive)
    from app.routing.circuit_breaker import clear_auth_failure as _clear_af
    _clear_af(provider.id)
    return _serialize(provider)


# ── v2.7.1: browser-initiated OAuth flow ────────────────────────────────────
# Admin clicks "Generate Auth URL", we return a PKCE-signed authorize URL they
# open in another tab, they approve on claude.ai, land on a dead
# http://localhost/callback page, copy the URL (or just the ?code=...), paste
# it back, and we exchange it for tokens + create the Provider row in one shot.


class OAuthAuthorizeResponse(BaseModel):
    state: str
    authorize_url: str


class OAuthExchangeRequest(BaseModel):
    # The state returned by /authorize.
    state: str
    # Either the full callback URL, the query fragment, or the bare ?code= value.
    callback: str
    # Provider fields to populate (same shape as ProviderCreate minus api_key/
    # oauth_credentials_blob — the access_token comes from the code exchange).
    name: str
    default_model: Optional[str] = None
    base_url: Optional[str] = None
    priority: int = 10
    enabled: bool = True
    timeout_sec: int = 30
    exclude_from_tool_requests: bool = False
    hold_down_sec: Optional[int] = None
    failure_threshold: Optional[int] = None
    daily_budget_usd: Optional[float] = None
    extra_config: dict = {}


@router.post("/claude-oauth/authorize", response_model=OAuthAuthorizeResponse)
async def claude_oauth_authorize(
    _: AdminUser = Depends(require_admin),
):
    """Start a Claude Pro Max OAuth flow. Returns the URL the admin opens."""
    from app.providers.claude_oauth_flow import start_authorize
    start = start_authorize()
    return OAuthAuthorizeResponse(state=start.state, authorize_url=start.authorize_url)


class OAuthRotateRequest(BaseModel):
    state: str
    callback: str


@router.post("/{provider_id}/oauth-rotate")
async def claude_oauth_rotate(
    provider_id: str,
    body: OAuthRotateRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Re-auth an existing claude-oauth provider in-place.

    v2.7.7: when a claude-oauth provider's tokens have been revoked
    server-side or the refresh_token chain breaks, the admin can hit
    "Re-auth" in the edit form, complete the browser flow, and paste
    back the CODE#STATE — this endpoint exchanges the code and updates
    the existing Provider row without creating a duplicate."""
    from app.providers.claude_oauth_flow import (
        exchange_code, extract_code_from_callback, OAuthFlowError,
    )
    p = await _get_or_404(db, provider_id)
    if p.provider_type != "claude-oauth":
        raise HTTPException(400, f"Provider {p.name!r} is not a claude-oauth provider")

    try:
        code, callback_state = extract_code_from_callback(body.callback)
    except ValueError as e:
        raise HTTPException(400, f"Couldn't parse callback: {e}")
    try:
        result = await exchange_code(body.state, code, expected_state=callback_state)
    except OAuthFlowError as e:
        raise HTTPException(400, str(e))

    p.api_key = result.access_token
    p.oauth_refresh_token = result.refresh_token
    p.oauth_expires_at = result.expires_at
    _stamp_user_edit(p)
    await db.commit()
    await db.refresh(p)
    register_provider(p.id, p.provider_type, p.hold_down_sec, p.failure_threshold)
    # v2.7.8 BUG-002: fresh tokens — clear stale auth-failure + close breaker
    from app.routing.circuit_breaker import clear_auth_failure as _clear_af, force_close
    _clear_af(p.id)
    await force_close(p.id)
    return _serialize(p)


@router.post("/claude-oauth/exchange")
async def claude_oauth_exchange(
    body: OAuthExchangeRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Exchange the callback code for tokens and create the Provider row."""
    from app.providers.claude_oauth_flow import (
        exchange_code, extract_code_from_callback, OAuthFlowError,
    )
    try:
        code, callback_state = extract_code_from_callback(body.callback)
    except ValueError as e:
        raise HTTPException(400, f"Couldn't parse callback: {e}")
    try:
        result = await exchange_code(body.state, code, expected_state=callback_state)
    except OAuthFlowError as e:
        raise HTTPException(400, str(e))

    # v3.0.12: same dedup guard as the standard create path.
    from app.providers.dedup import name_is_taken
    if await name_is_taken(db, body.name):
        raise HTTPException(
            409,
            f"A provider named {body.name!r} already exists.",
        )

    data = body.model_dump(exclude={"state", "callback"})
    data["provider_type"] = "claude-oauth"
    data["api_key"] = result.access_token
    data["oauth_refresh_token"] = result.refresh_token
    data["oauth_expires_at"] = result.expires_at
    if not data.get("default_model"):
        data["default_model"] = "claude-sonnet-4-6"

    provider = Provider(id=secrets.token_hex(8), **data)
    _stamp_user_edit(provider)
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    register_provider(
        provider.id, provider.provider_type, provider.hold_down_sec, provider.failure_threshold,
    )
    return _serialize(provider)


@router.get("/{provider_id}")
async def get_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    return _serialize(p)


@router.put("/{provider_id}")
async def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    data = body.model_dump()
    blob = data.pop("oauth_credentials_blob", None)

    # v2.7.0: re-paste credentials to refresh an existing claude-oauth provider
    if blob and p.provider_type == "claude-oauth":
        from app.providers.claude_oauth import parse_credentials, CredentialParseError
        try:
            creds = parse_credentials(blob)
        except CredentialParseError as e:
            raise HTTPException(400, f"Credential parse failed: {e}")
        data["api_key"] = creds.access_token
        data["oauth_refresh_token"] = creds.refresh_token
        data["oauth_expires_at"] = creds.expires_at
    elif blob:
        raise HTTPException(
            400,
            f"oauth_credentials_blob is only valid for claude-oauth providers "
            f"(this one is {p.provider_type!r})",
        )

    # For claude-oauth without a re-paste, keep existing api_key + oauth_* fields.
    # If admin sent api_key="" on a claude-oauth update, ignore (they didn't mean
    # to wipe it).
    if p.provider_type == "claude-oauth" and not blob:
        data.pop("api_key", None)

    # v2.7.8 BUG-002: if admin pasted a new api_key OR blob, clear the
    # auth-failure flag so the provider gets a fresh chance.
    new_key_provided = (
        ("api_key" in data and data["api_key"])  # non-empty api_key on the update
        or blob  # claude-oauth re-paste
    )

    # v2.8.2: if priority changed, bump any other provider already at the new
    # priority +1 (chain-reaction) so this update takes the slot it asked for.
    new_priority = data.get("priority")
    if new_priority is not None and new_priority != p.priority:
        await _bump_priority_conflicts(db, new_priority, exclude_id=p.id)

    # v3.0.12: reject renames that would collide with another active row.
    new_name = data.get("name")
    if new_name and new_name != p.name:
        from app.providers.dedup import name_is_taken
        if await name_is_taken(db, new_name, exclude_id=p.id):
            raise HTTPException(
                409,
                f"Another provider already uses the name {new_name!r}.",
            )

    for field, value in data.items():
        setattr(p, field, value)
    _stamp_user_edit(p)
    await db.commit()
    await db.refresh(p)
    register_provider(p.id, p.provider_type, p.hold_down_sec, p.failure_threshold)
    if new_key_provided:
        from app.routing.circuit_breaker import clear_auth_failure as _clear_af, force_close
        _clear_af(p.id)
        await force_close(p.id)
    return _serialize(p)


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v2.8.2: soft-delete via tombstone.

    Hard DELETE used to be reversed by cluster sync — peers still had the
    row and apply_sync re-inserted it. Now we set deleted_at = now() and
    flip enabled=False; sync compares updated_at on the tombstone too, so
    the delete propagates to peers. Garbage collection of old tombstones
    is a separate background sweep (TODO: 7-day retention).
    """
    from datetime import datetime, timezone
    p = await _get_or_404(db, provider_id)
    p.deleted_at = datetime.now(timezone.utc)
    p.enabled = False
    # Bump updated_at so cluster sync recognizes this as the freshest write
    p.updated_at = datetime.now(timezone.utc)
    _stamp_user_edit(p)
    await db.commit()
    return {"ok": True}


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
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    p.enabled = not p.enabled
    _stamp_user_edit(p)
    await db.commit()
    return {"enabled": p.enabled}


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
        from app.providers.deprecations import MODEL_DEPRECATIONS
        deprecated_models = [
            {"id": m, "replacement": MODEL_DEPRECATIONS[m]}
            for m in (models or [])
            if m in MODEL_DEPRECATIONS
        ]
        out = {"scanned": len(models), "models": models}
        if not models:
            out["warning"] = "No models discovered — check API key and provider type"
        if deprecated_models:
            out["deprecated_models"] = deprecated_models
        return out
    except Exception as e:
        raise HTTPException(500, f"Model scan failed: {e}")


@router.get("/{provider_id}/model-capabilities")
async def list_capabilities(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(
        select(ModelCapability).where(ModelCapability.provider_id == provider_id)
    )
    caps = result.scalars().all()
    return [_serialize_cap(c) for c in caps]


@router.put("/{provider_id}/model-capabilities/{model_id:path}")
async def upsert_capability(
    provider_id: str,
    model_id: str,
    body: CapabilityUpdate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(
        select(ModelCapability).where(
            ModelCapability.provider_id == provider_id,
            ModelCapability.model_id == model_id,
        )
    )
    cap = result.scalar_one_or_none()
    if cap:
        for f, v in body.model_dump().items():
            setattr(cap, f, v)
        cap.source = "manual"
    else:
        cap = ModelCapability(
            provider_id=provider_id,
            model_id=model_id,
            source="manual",
            **body.model_dump(),
        )
        db.add(cap)
    await db.commit()
    await db.refresh(cap)
    return _serialize_cap(cap)


@router.post("/{provider_id}/model-capabilities/infer")
async def infer_capabilities(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Re-run auto-inference on all existing capability records for this provider."""
    p = await _get_or_404(db, provider_id)
    result = await db.execute(
        select(ModelCapability).where(
            ModelCapability.provider_id == provider_id,
            ModelCapability.source == "inferred",
        )
    )
    caps = result.scalars().all()
    updated = 0
    for cap in caps:
        profile = infer_capability_profile(provider_id, p.provider_type, cap.model_id, p.priority)
        cap.tasks = profile.tasks
        cap.latency = profile.latency
        cap.cost_tier = profile.cost_tier
        cap.safety = profile.safety
        cap.context_length = profile.context_length
        cap.regions = profile.regions
        cap.modalities = profile.modalities
        cap.native_reasoning = profile.native_reasoning
        updated += 1
    await db.commit()
    return {"updated": updated}


async def _get_or_404(db: AsyncSession, provider_id: str) -> Provider:
    result = await db.execute(select(Provider).where(Provider.id == provider_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Provider not found")
    return p


def _serialize(p: Provider) -> dict:
    # v2.7.8 BUG-002: surface a "needs re-auth" flag the UI can render as a
    # red badge. Reads from the in-process auth-failure map maintained by
    # circuit_breaker.record_auth_failure. None when the provider is healthy.
    from app.routing.circuit_breaker import get_auth_failure
    auth_fail = get_auth_failure(p.id)
    return {
        "id": p.id,
        "name": p.name,
        "provider_type": p.provider_type,
        "api_key": f"{p.api_key[:8]}..." if p.api_key else None,
        "base_url": p.base_url,
        "default_model": p.default_model,
        "priority": p.priority,
        "enabled": p.enabled,
        "timeout_sec": p.timeout_sec,
        "exclude_from_tool_requests": p.exclude_from_tool_requests,
        "hold_down_sec": p.hold_down_sec,
        "failure_threshold": p.failure_threshold,
        "daily_budget_usd": p.daily_budget_usd,
        "extra_config": p.extra_config,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        # v2.7.0: expose expiry so the UI can show "Token expires in Nh"
        # for claude-oauth providers. Never expose refresh_token.
        "oauth_expires_at": p.oauth_expires_at,
        "has_oauth_refresh_token": bool(p.oauth_refresh_token),
        # v2.7.8: auth-failure state. Frontend renders a red "Needs re-auth"
        # badge when this is non-null; admin clears via re-key save or
        # POST /api/providers/{id}/clear-auth-failure.
        "auth_failed": auth_fail,
    }


def _serialize_cap(c: ModelCapability) -> dict:
    return {
        "id": c.id,
        "provider_id": c.provider_id,
        "model_id": c.model_id,
        "tasks": c.tasks,
        "latency": c.latency,
        "cost_tier": c.cost_tier,
        "safety": c.safety,
        "context_length": c.context_length,
        "regions": c.regions,
        "modalities": c.modalities,
        "native_reasoning": c.native_reasoning,
        "native_tools": c.native_tools,
        "native_vision": c.native_vision,
        "source": c.source,
    }
