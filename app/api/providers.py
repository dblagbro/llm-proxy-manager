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
from app.utils.timefmt import utc_iso

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
    # v3.0.45: tenant scoping. Null = shared across all keys (default).
    # Set to an api_keys.id to restrict this provider to a single key.
    owned_by_key_id: Optional[str] = None
    # v2.7.0: claude-oauth credential paste. The frontend sends either:
    #   - bare ``sk-ant-oat...`` access token, OR
    #   - the JSON contents of ``~/.claude/credentials.json``.
    # Server parses, extracts access/refresh/expires_at, and stores them in
    # the existing api_key column + new oauth_* columns. The raw blob is
    # never persisted.
    oauth_credentials_blob: Optional[str] = None
    # v3.0.64: per-provider usage-based rotation config (Phase 2). All
    # optional — providers default to "no tracking" until operator opts in.
    usage_tracking_enabled: Optional[bool] = None
    usage_session_window_sec: Optional[int] = None
    usage_weekly_reset_dow: Optional[int] = None
    usage_weekly_reset_hour: Optional[int] = None
    usage_session_limit_tokens: Optional[int] = None
    usage_weekly_limit_tokens: Optional[int] = None
    usage_rotation_threshold_pct: Optional[int] = None


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
    # v3.5.1 — model-identity fields exposed for operator edit via the
    # Hub capability admin form. Optional + defaulted so older Hub UI
    # clients that don't send them still PUT successfully (back-compat
    # with the v3.4.1 schema where these defaulted to []/null/null at
    # the column level).
    aliases: list[str] = []
    model_family: Optional[str] = None
    model_variant: Optional[str] = None


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
    # v3.0.64: bulk-load usage windows so the list page can show per-provider
    # session_pct + weekly_pct without an N+1.
    from app.models.db import ProviderUsageWindow
    usage_res = await db.execute(select(ProviderUsageWindow))
    usage_by_id = {w.provider_id: w for w in usage_res.scalars().all()}
    out = []
    for p in providers:
        d = _serialize(p)
        w = usage_by_id.get(p.id)
        if w:
            d["usage_session_pct"] = w.session_pct
            d["usage_session_tokens"] = w.session_tokens
            d["usage_weekly_pct"] = w.weekly_pct
            d["usage_weekly_tokens"] = w.weekly_tokens
        out.append(d)
    return out


@router.get("/rolling-stats")
async def provider_rolling_stats(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.0.39: per-provider request volume + success rate across rolling
    1h / 24h / 7d / 30d windows. Backs the new columns on the provider list
    page (operator ask 2026-05-01).

    Returns: list of {provider_id, provider_name, windows: {1h, 24h, 7d, 30d}}
    where each window has {requests, successes, success_pct}. Providers with
    no traffic in the 30d window are omitted; the frontend treats absence as
    'no data'.
    """
    from app.monitoring.metrics import get_provider_rolling_windows
    return await get_provider_rolling_windows(db)


@router.get("/{provider_id}/usage")
async def provider_usage(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.0.62: per-provider rolling usage windows (Phase 1 of usage-based
    rotation). Returns cached values from ``provider_usage_windows`` —
    populated by the ``usage_tracker`` background task every 60s for
    providers with ``usage_tracking_enabled=True``.

    Response shape:
      {
        provider_id, provider_name, tracking_enabled,
        session: {tokens, window_start, window_sec, limit_tokens, pct},
        weekly:  {tokens, reset_at, reset_dow, reset_hour, limit_tokens, pct},
        rotation_threshold_pct,
        updated_at,
      }

    If tracking is disabled, returns the config + null totals so the UI
    can show the "enable tracking" affordance.
    """
    from app.models.db import ProviderUsageWindow
    from sqlalchemy import select as _sel
    res = await db.execute(_sel(Provider).where(
        Provider.id == provider_id, Provider.deleted_at.is_(None),
    ))
    p = res.scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "provider not found")

    res2 = await db.execute(_sel(ProviderUsageWindow).where(
        ProviderUsageWindow.provider_id == provider_id,
    ))
    w = res2.scalar_one_or_none()

    return {
        "provider_id": p.id,
        "provider_name": p.name,
        "tracking_enabled": bool(p.usage_tracking_enabled),
        "session": {
            "tokens": (w.session_tokens if w else 0),
            # v3.0.82: utc_iso() instead of naive ``isoformat() + "Z"``.
            # session_window_start is written by usage_tracker via
            # datetime.now(timezone.utc) so it's tz-aware; the bare
            # concat produced ``"2026-05-06T05:00:00+00:00Z"`` which JS
            # Date won't parse cleanly. Same fix as v3.0.73 utc_iso bug.
            "window_start": utc_iso(w.session_window_start) if w else None,
            "window_sec": p.usage_session_window_sec,
            "limit_tokens": p.usage_session_limit_tokens,
            "pct": (w.session_pct if w else None),
        },
        "weekly": {
            "tokens": (w.weekly_tokens if w else 0),
            "reset_at": utc_iso(w.weekly_reset_at) if w else None,
            "reset_dow": p.usage_weekly_reset_dow,
            "reset_hour": p.usage_weekly_reset_hour,
            "limit_tokens": p.usage_weekly_limit_tokens,
            "pct": (w.weekly_pct if w else None),
        },
        "rotation_threshold_pct": p.usage_rotation_threshold_pct,
        "updated_at": utc_iso(w.updated_at) if w else None,
    }


_TYPES_REQUIRING_API_KEY = {
    "anthropic", "openai", "google", "vertex", "grok",
    "cohere", "mistral", "groq", "together", "fireworks",
    # v3.0.66: Azure OpenAI requires both api_key + base_url (the
    # resource endpoint). base_url is enforced by the form; api_key
    # via this set.
    "azure",
    # v3.1.3: OpenRouter — bearer token (sk-or-v1-…). base_url is
    # implicit (litellm uses https://openrouter.ai/api/v1 internally).
    "openrouter",
}


async def normalize_priority_ties(db: AsyncSession) -> int:
    """v2.8.2 / v3.0.24: resolve any priority ties among ROUTABLE providers
    by bumping the younger duplicates +1 in created_at order. Idempotent.
    Returns count of providers bumped (0 means already normalized).

    v3.0.24 (#136): scope changed to ``deleted_at IS NULL AND enabled=True``.
    Tombstoned and disabled rows don't participate in routing — letting them
    contribute to tie detection caused observed ping-pong on www01 (45 fires
    in 3h with count=2 each, while peers fired 0). Logs the bumped row IDs
    so future debugging knows what moved.

    Example before: [a@1, b@2, c@2, d@3, e@3] (b created before c, d before e)
    After: [a@1, b@2, c@3, d@4, e@5] — younger row at each tie shifts up,
    and the cascade from c=3 collides with d=3, so d→4. The net effect is
    a strict total order by (priority, created_at) within the active set.
    """
    from sqlalchemy import select as _select
    rows = (await db.execute(
        _select(Provider)
        .where(Provider.deleted_at.is_(None), Provider.enabled == True)
        .order_by(Provider.priority.asc(), Provider.created_at.asc(), Provider.id.asc())
    )).scalars().all()
    bumped: list[tuple[str, int, int]] = []   # (id, old, new)
    seen_priorities: set[int] = set()
    for row in rows:
        if row.priority not in seen_priorities:
            seen_priorities.add(row.priority)
            continue
        # Tie — find the next free slot at or above row.priority
        old_pri = row.priority
        new_pri = old_pri
        while new_pri in seen_priorities:
            new_pri += 1
        row.priority = new_pri
        seen_priorities.add(new_pri)
        bumped.append((row.id, old_pri, new_pri))
    if bumped:
        await db.flush()
        try:
            import logging as _logging
            _logging.getLogger(__name__).info(
                "providers.normalize_priority_ties bumped=%d details=%s",
                len(bumped),
                [{"id": pid, "from": o, "to": n} for (pid, o, n) in bumped],
            )
        except Exception:
            pass
    return len(bumped)


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

    # v3.8.0 (#251) — backward-compat alias for the codex-oauth → ChatGPT-oauth-plan
    # rename. Callers that POST the old name still work; we normalize to the
    # new name before storing. Drop this shim in a future major version once
    # all known callers have updated.
    if body.provider_type == "codex-oauth":
        body.provider_type = "ChatGPT-oauth-plan"
        data["provider_type"] = "ChatGPT-oauth-plan"

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
    elif body.provider_type == "ChatGPT-oauth-plan":
        # v3.0.15: OpenAI Codex CLI / ChatGPT subscription OAuth.
        if not blob:
            raise HTTPException(
                400,
                "codex-oauth providers require 'oauth_credentials_blob' — paste your "
                "`~/.codex/auth.json` contents or a bare access_token JWT.",
            )
        from app.providers.codex_oauth import (
            parse_credentials as _parse_codex, CredentialParseError as _CodexParseErr,
        )
        try:
            creds = _parse_codex(blob)
        except _CodexParseErr as e:
            raise HTTPException(400, f"Credential parse failed: {e}")
        data["api_key"] = creds.access_token
        data["oauth_refresh_token"] = creds.refresh_token
        data["oauth_expires_at"] = creds.expires_at
        # Stash workspace + plan-tier in extra_config so the dispatcher can
        # stamp ChatGPT-Account-ID without re-decoding the JWT every call.
        cfg = dict(data.get("extra_config") or {})
        if creds.chatgpt_account_id:
            cfg["chatgpt_account_id"] = creds.chatgpt_account_id
        if creds.chatgpt_plan_type:
            cfg["chatgpt_plan_type"] = creds.chatgpt_plan_type
        data["extra_config"] = cfg
        if not data.get("default_model"):
            data["default_model"] = "gpt-5.5"
    elif body.provider_type == "grok-web":
        # v3.2.0: grok.com web-subscription provider. Two valid auth paths
        # captured in extra_config:
        #
        #   Bridge mode (v3.2.1+): bridge_url is set → the Playwright sidecar
        #     holds the live cookies. We only require conversation_id here;
        #     cookie_header is irrelevant (bridge captures it from its own
        #     logged-in browser session).
        #
        #   Manual mode (legacy): operator pastes cookie_header + conversation_id
        #     directly. Cookies live on this Provider row and we do HTTP replay
        #     from the dispatcher.
        cfg = data.get("extra_config") or {}
        is_bridge = bool((cfg.get("bridge_url") or "").strip())
        if is_bridge:
            if not (cfg.get("conversation_id") or "").strip():
                raise HTTPException(
                    400,
                    "grok-web bridge mode requires extra_config.conversation_id "
                    "(an existing grok.com conversation UUID — the bit after "
                    "grok.com/c/ in your browser's URL bar).",
                )
        else:
            missing = [k for k in ("cookie_header", "conversation_id") if not (cfg.get(k) or "").strip()]
            if missing:
                raise HTTPException(
                    400,
                    f"grok-web providers require extra_config fields {missing}. "
                    "Easiest path: switch to Bridge mode in the form (one-time "
                    "browser login, no cookie pasting). Or: in a logged-in "
                    "browser at grok.com, copy a fetch as cURL — paste the cookie "
                    "header into 'cookie_header' and the UUID from the URL "
                    "(grok.com/c/<this-uuid>) into 'conversation_id'.",
                )
        if not data.get("default_model"):
            data["default_model"] = "grok-3"
    elif blob:
        raise HTTPException(
            400,
            f"oauth_credentials_blob is only valid when provider_type is 'claude-oauth' "
            f"or 'ChatGPT-oauth-plan' (got {body.provider_type!r})",
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


# ── OAuth flow endpoints relocated to providers_oauth.py ────────────────────
# claude-oauth + codex-oauth authorize / exchange / rotate live in
# ``app/api/providers_oauth.py``. ~250 lines of near-duplicate code (the
# two flows differed only in vendor — same PKCE pattern, same exchange
# shape, same Provider-row mutations) became a 6-endpoint shell over a
# parameterized ``OAuthProviderSpec``. Adding a third OAuth provider type
# (Vertex, Azure-AD, Bedrock) is now ~30 lines instead of a 200-line
# copy-paste. Routes still live under ``/api/providers/...`` — registered
# alongside this router in ``app/main.py``.


@router.get("/{provider_id}")
async def get_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    return _serialize(p)


@router.get("/{provider_id}/rate-limit")
async def get_rate_limit_state(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.0.16: subscription-quota window state for OAuth-based providers.

    Codex returns x-codex-* headers on every response with how much of the
    primary (5h) and secondary (weekly) quotas have been used and when each
    resets. We track these in-memory per provider. Admin UI uses this for
    the "X% used, resets in Y" display + ops dashboards.

    Returns ``{"observed": false}`` if no Codex response has been seen on
    this node yet (cold cache / never-used provider).
    """
    p = await _get_or_404(db, provider_id)
    if p.provider_type != "ChatGPT-oauth-plan":
        raise HTTPException(
            400,
            f"Provider {p.name!r} is type {p.provider_type!r}; rate-limit "
            "state is only tracked for codex-oauth.",
        )
    from app.providers.codex_ratelimit import get_state
    state = get_state(provider_id)
    if state is None:
        return {"provider_id": provider_id, "observed": False}
    import time as _t
    now = _t.time()
    return {
        "provider_id": provider_id,
        "observed": True,
        "plan_type": state.plan_type,
        "active_limit": state.active_limit,
        "primary": {
            "used_percent": state.primary_used_percent,
            "window_minutes": state.primary_window_minutes,
            "reset_at": state.primary_reset_at,
            "reset_in_sec": (state.primary_reset_at - now) if state.primary_reset_at else None,
        },
        "secondary": {
            "used_percent": state.secondary_used_percent,
            "window_minutes": state.secondary_window_minutes,
            "reset_at": state.secondary_reset_at,
            "reset_in_sec": (state.secondary_reset_at - now) if state.secondary_reset_at else None,
        },
        "last_observed_at": state.last_observed_at,
        "last_observed_age_sec": now - state.last_observed_at,
    }


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

    # For OAuth-based providers without a re-paste, keep the existing api_key +
    # oauth_* fields. If admin sent api_key="" on the update, ignore — they
    # didn't mean to wipe it (the OAuth UI hides the api_key field entirely,
    # so an empty value here is just the form default, not an admin choice).
    # v3.0.15: extended from claude-oauth to also cover codex-oauth.
    # v3.0.16 (#129): also merge extra_config instead of replacing. The
    # rotate endpoint stashes chatgpt_account_id / chatgpt_plan_type into
    # extra_config; the edit form takes its snapshot of extra_config at
    # modal-open, BEFORE rotate runs. If we let the PUT overwrite, the
    # rotate's freshly-stashed keys get clobbered by the stale snapshot.
    # Solution: layer the incoming form values OVER the current row's
    # extra_config so OAuth-stashed keys survive while admin-added keys
    # still win.
    if p.provider_type in ("claude-oauth", "ChatGPT-oauth-plan") and not blob:
        data.pop("api_key", None)
        incoming_extra = data.pop("extra_config", None)
        if incoming_extra is not None:
            merged = dict(p.extra_config or {})
            merged.update(incoming_extra)
            data["extra_config"] = merged

    # v3.1.6: NEVER silently clear an api_key on PUT for ANY provider type.
    # Earlier guard above covered claude-oauth/codex-oauth only — but the
    # frontend ProviderForm sends api_key as a masked-display value (or
    # blank when redisplaying an existing provider), so editing priority
    # on an OpenRouter / openai / anthropic / etc. provider via the UI
    # silently dropped the key. Operator hit this twice on OpenRouter
    # in one day. If admin really wants to clear an api_key they should
    # delete + recreate the provider.
    #
    # Heuristics for "this isn't a real new key":
    #   - field omitted (None)
    #   - empty string
    #   - a masked-display sentinel (contains "…", "..." or matches the
    #     UI's redacted pattern: starts with the prefix + "..." or "***")
    if "api_key" in data:
        incoming = data.get("api_key")
        if incoming is None or incoming == "" or (
            isinstance(incoming, str) and (
                "…" in incoming or "***" in incoming or
                # First 8 chars of existing + "..." pattern (UI redact)
                (p.api_key and incoming.startswith(p.api_key[:8]) and ("..." in incoming or "…" in incoming))
            )
        ):
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
    the delete propagates to peers. v3.0.13: tombstones older than
    ``provider_tombstone_retention_days`` are hard-deleted by the daily
    prune worker (default 7 days).
    """
    from datetime import datetime, timezone
    p = await _get_or_404(db, provider_id)
    p.deleted_at = datetime.now(timezone.utc)
    p.enabled = False
    # Bump updated_at so cluster sync recognizes this as the freshest write
    p.updated_at = datetime.now(timezone.utc)
    _stamp_user_edit(p)
    await db.commit()
    # v3.5.9 BUG-012 fix — clear in-memory circuit-breaker state for the
    # deleted provider. Pre-fix the CB ``_local_states`` dict held the
    # entry indefinitely, so /health reported ghost CBs (open/half-open
    # states) for providers that no longer existed. Most visible via
    # the integration tests' pytest-mock leftovers — see docs/bug-log.md
    # BUG-003 + BUG-012 for context.
    from app.routing.circuit_breaker import (
        _local_states as _cb_states,
        _auth_failed as _cb_auth_failed,
    )
    _cb_states.pop(provider_id, None)
    _cb_auth_failed.pop(provider_id, None)
    return {"ok": True}


@router.post("/_purge-test-tombstones")
async def purge_test_tombstones(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.5.11 BUG-003 fix — hard-delete tombstoned providers whose name
    matches a test pattern AND whose ``deleted_at`` is older than 60s
    (cluster sync convergence buffer).

    Mirrors ``/api/keys/_purge-test-tombstones`` (which exists for
    api_keys). Without a parallel for providers, every integration
    test run that creates ``pytest-mock`` rows leaves them
    soft-deleted for the full 7-day tombstone retention window,
    bloating cluster_sync payloads and confusing operators reading
    the providers table directly.

    Used by integration test ``pytest_sessionfinish`` hook. Safe in
    production: only affects providers named ``pytest-%`` /
    ``test-playwright-%`` / ``debug-%``. Admin-gated.
    """
    from sqlalchemy import delete, or_, func
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
    patterns = ("pytest-%", "test-playwright-%", "debug-%")
    rs = await db.execute(
        delete(Provider)
        .where(Provider.deleted_at.is_not(None))
        .where(Provider.deleted_at < cutoff)
        .where(or_(*[Provider.name.like(p) for p in patterns]))
    )
    purged = rs.rowcount
    await db.commit()
    # Also clean any orphan CB / auth-failed state for the deleted
    # ids — soft-delete already triggered the cleanup, but if those
    # rows were soft-deleted before v3.5.9 the CB state may still
    # be there. This pass is idempotent.
    from app.routing.circuit_breaker import (
        _local_states as _cb_states,
        _auth_failed as _cb_auth_failed,
    )
    # Take a snapshot of current CB ids; remove any that aren't still
    # present in providers table.
    if _cb_states or _cb_auth_failed:
        live_ids = {
            row[0] for row in (
                await db.execute(select(Provider.id).where(Provider.deleted_at.is_(None)))
            ).all()
        }
        for ghost in [pid for pid in list(_cb_states) if pid not in live_ids]:
            _cb_states.pop(ghost, None)
        for ghost in [pid for pid in list(_cb_auth_failed) if pid not in live_ids]:
            _cb_auth_failed.pop(ghost, None)
    return {"ok": True, "purged": purged}


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
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.7.28 (#252 phase 1): bulk-clear manual override on all
    providers — the "Release all to AI control" banner button. Does
    NOT change ``enabled``; only releases the lock so the supervisor
    can manage the providers again. If a provider is currently
    disabled by manual override and the operator releases it, the
    supervisor will see ``enabled=False`` + ``manual_override_until=NULL``
    and treat it like any other disabled provider (may re-enable
    based on its verdict).
    """
    from sqlalchemy import update
    from app.models.db import Provider
    result = await db.execute(
        update(Provider)
        .where(Provider.manual_override_until.is_not(None))
        .where(Provider.deleted_at.is_(None))
        .values(
            manual_override_until=None,
            manual_override_set_by=None,
            manual_override_set_at=None,
            manual_override_reason=None,
        )
    )
    await db.commit()
    return {"released": result.rowcount}


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
        # v3.0.45: provider tenant scoping. Null = shared (default behavior);
        # set to an api_keys.id to restrict routing to that key only.
        "owned_by_key_id": p.owned_by_key_id,
        "created_at": utc_iso(p.created_at),
        # v2.7.0: expose expiry so the UI can show "Token expires in Nh"
        # for claude-oauth providers. Never expose refresh_token.
        "oauth_expires_at": p.oauth_expires_at,
        "has_oauth_refresh_token": bool(p.oauth_refresh_token),
        # v2.7.8: auth-failure state. Frontend renders a red "Needs re-auth"
        # badge when this is non-null; admin clears via re-key save or
        # POST /api/providers/{id}/clear-auth-failure.
        "auth_failed": auth_fail,
        # v3.0.64: usage-based rotation config (Phase 2). UI uses these
        # to render the per-provider usage section in the edit form.
        "usage_tracking_enabled": bool(p.usage_tracking_enabled),
        "usage_session_window_sec": p.usage_session_window_sec,
        "usage_weekly_reset_dow": p.usage_weekly_reset_dow,
        "usage_weekly_reset_hour": p.usage_weekly_reset_hour,
        "usage_session_limit_tokens": p.usage_session_limit_tokens,
        "usage_weekly_limit_tokens": p.usage_weekly_limit_tokens,
        "usage_rotation_threshold_pct": p.usage_rotation_threshold_pct,
        # v3.7.0/v3.7.1 — external billing scrape + auto-rotation
        # diagnostic surface. Operator UI / admin endpoints show
        # whether the provider is currently in an auto-skip window
        # and why. Cookies themselves are NEVER surfaced.
        "anthropic_org_uuid": p.anthropic_org_uuid,
        "has_anthropic_session_cookies": bool(p.anthropic_session_cookies),
        "anthropic_session_captured_at": p.anthropic_session_captured_at,
        # v3.7.27 (#245) — Codex / ChatGPT Plus billing scrape state.
        # Same shape as the Anthropic fields above. The cookies blob
        # itself is NEVER surfaced; only the endpoint URL the operator
        # captured + the captured-at timestamp.
        "codex_usage_endpoint_url": getattr(p, "codex_usage_endpoint_url", None),
        "has_codex_session_cookies": bool(getattr(p, "codex_session_cookies", None)),
        "codex_session_captured_at": getattr(p, "codex_session_captured_at", None),
        # v3.7.28 (#252 phase 1) — manual override state. When the UI
        # banner detects ``manual_override_active`` on any provider it
        # surfaces the top-of-page warning. The supervisor (Phase 4)
        # reads ``manual_override_until`` directly from the row.
        "manual_override_active": getattr(p, "manual_override_until", None) is not None,
        "manual_override_until": utc_iso(p.manual_override_until) if getattr(p, "manual_override_until", None) else None,
        "manual_override_set_by": getattr(p, "manual_override_set_by", None),
        "manual_override_set_at": utc_iso(p.manual_override_set_at) if getattr(p, "manual_override_set_at", None) else None,
        "manual_override_reason": getattr(p, "manual_override_reason", None),
        "auto_skip_until": utc_iso(p.auto_skip_until) if p.auto_skip_until else None,
        "auto_skip_reason": p.auto_skip_reason,
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
        # v3.5.1 — surface the model-identity fields to the Hub UI so
        # the capability admin form can show + edit them.
        "aliases": c.aliases or [],
        "model_family": c.model_family,
        "model_variant": c.model_variant,
    }
