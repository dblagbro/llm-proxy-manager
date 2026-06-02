"""OAuth-flow endpoints for ``claude-oauth`` + ``codex-oauth`` provider types.

Both vendors follow the same browser-PKCE pattern:

  1. Admin clicks "Generate Auth URL"; we return a PKCE-signed authorize URL.
  2. Admin opens it, approves on the vendor's site, lands on a dead-end
     localhost callback URL.
  3. Admin copies the URL (or just the ``?code=…`` value) and pastes it
     back; we exchange it for tokens and create-or-update the Provider row.

The two flows differ only in vendor-specific details captured in
``OAuthProviderSpec``:

  - ``provider_type`` column value (``"claude-oauth"`` / ``"ChatGPT-oauth-plan"``)
  - flow module (``app.providers.claude_oauth_flow`` /
    ``app.providers.codex_oauth_flow``) — both expose the same surface:
    ``start_authorize() -> AuthorizeStart``,
    ``extract_code_from_callback(s) -> (code, state)``,
    ``exchange_code(state, code, expected_state=...) -> ExchangeResult``,
    and the ``OAuthFlowError`` exception type.
  - ``default_model`` fallback when caller didn't specify one
  - which result fields get stashed in ``extra_config`` (codex stashes
    ``chatgpt_account_id`` + ``chatgpt_plan_type``; claude has none).

Extracted from ``app/api/providers.py`` (which was 1136 lines) in the
v3.0.x architectural refactor. ~250 lines of near-duplicate code became
~190 lines of shared logic plus a 6-endpoint shell.
"""
from __future__ import annotations

import importlib
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import Provider
from app.monitoring.status import register_provider

# All OAuth endpoints share the providers prefix so they appear under
# /api/providers/... alongside the CRUD / test / scan endpoints.
router = APIRouter(prefix="/api/providers", tags=["providers", "oauth"])


# ── Per-vendor spec ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthProviderSpec:
    """Vendor-specific knobs the shared handlers need.

    ``flow_module_name`` is imported lazily so the worker process doesn't
    pull in OAuth-flow code paths until an admin actually clicks the
    button.
    """
    provider_type: str
    default_model: str
    flow_module_name: str
    # Names of attributes on the ExchangeResult to copy into ``extra_config``.
    extra_config_keys: tuple[str, ...] = ()


CLAUDE_OAUTH_SPEC = OAuthProviderSpec(
    provider_type="claude-oauth",
    default_model="claude-sonnet-4-6",
    flow_module_name="app.providers.claude_oauth_flow",
)


CODEX_OAUTH_SPEC = OAuthProviderSpec(
    provider_type="ChatGPT-oauth-plan",
    default_model="gpt-5.5",
    flow_module_name="app.providers.codex_oauth_flow",
    # Codex tokens carry the workspace/account/plan-tier metadata in
    # the JWT; we stash it in extra_config so the dispatcher can stamp
    # ``ChatGPT-Account-ID`` without re-decoding the JWT every call.
    extra_config_keys=("chatgpt_account_id", "chatgpt_plan_type"),
)


# v4.4.31 — Cursor (Pro/Business subscription via the
# Cursor-To-OpenAI sidecar at llm-proxy2-cursor-bridge).
# See app/providers/cursor_oauth_flow.py for the exchange dance and
# docs/cursor-oauth-onboarding.md for the operator-facing flow.
CURSOR_OAUTH_SPEC = OAuthProviderSpec(
    provider_type="cursor-oauth",
    default_model="claude-3-7-sonnet",
    flow_module_name="app.providers.cursor_oauth_flow",
)


def _flow_module(spec: OAuthProviderSpec):
    """Return the per-vendor flow module (lazy import)."""
    return importlib.import_module(spec.flow_module_name)


# ── Request / response models (shared across both vendors) ───────────────────


class OAuthAuthorizeResponse(BaseModel):
    state: str
    authorize_url: str


class OAuthExchangeRequest(BaseModel):
    """Same shape as ``ProviderCreate`` minus ``api_key`` /
    ``oauth_credentials_blob`` — the access_token comes from the code
    exchange."""
    state: str
    callback: str  # full callback URL, query fragment, or bare ?code=… value
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


class OAuthRotateRequest(BaseModel):
    state: str
    callback: str


# ── Shared inner handlers (parameterized by spec) ────────────────────────────


def _do_authorize(spec: OAuthProviderSpec) -> OAuthAuthorizeResponse:
    flow = _flow_module(spec)
    start = flow.start_authorize()
    return OAuthAuthorizeResponse(state=start.state, authorize_url=start.authorize_url)


async def _do_exchange_create(
    spec: OAuthProviderSpec,
    body: OAuthExchangeRequest,
    db: AsyncSession,
) -> dict:
    """Exchange the callback code for tokens and CREATE a new Provider row.

    Used by ``POST /claude-oauth/exchange`` and ``POST /codex-oauth/exchange``.
    """
    flow = _flow_module(spec)
    try:
        code, callback_state = flow.extract_code_from_callback(body.callback)
    except ValueError as e:
        raise HTTPException(400, f"Couldn't parse callback: {e}")
    try:
        result = await flow.exchange_code(
            body.state, code, expected_state=callback_state,
        )
    except flow.OAuthFlowError as e:
        raise HTTPException(400, str(e))

    # v3.0.12 dedup guard — same as the standard create path.
    from app.providers.dedup import name_is_taken
    if await name_is_taken(db, body.name):
        raise HTTPException(
            409, f"A provider named {body.name!r} already exists.",
        )

    data = body.model_dump(exclude={"state", "callback"})
    data["provider_type"] = spec.provider_type
    data["api_key"] = result.access_token
    data["oauth_refresh_token"] = result.refresh_token
    data["oauth_expires_at"] = result.expires_at
    if not data.get("default_model"):
        data["default_model"] = spec.default_model

    # v4.4.31 — cursor-oauth Providers always dispatch through the
    # Cursor-To-OpenAI sidecar; the operator never edits the base_url.
    # Pin it here so the create+rotate paths share one definition. If
    # the operator did pass a value (e.g. for a non-default sidecar
    # name), respect it.
    if spec.provider_type == "cursor-oauth" and not data.get("base_url"):
        import os as _os
        data["base_url"] = _os.environ.get(
            "CURSOR_BRIDGE_URL",
            "http://llm-proxy2-cursor-bridge:3010",
        ).rstrip("/") + "/v1"

    # Vendor-specific: copy result fields into extra_config (codex carries
    # chatgpt_account_id / chatgpt_plan_type; claude carries nothing extra).
    if spec.extra_config_keys:
        cfg = dict(data.get("extra_config") or {})
        for key in spec.extra_config_keys:
            value = getattr(result, key, None)
            if value:
                cfg[key] = value
        data["extra_config"] = cfg

    # Helpers live in providers.py; import locally to avoid a circular
    # import at module load time.
    from app.api.providers import (
        _bump_priority_conflicts, _stamp_user_edit, _serialize,
    )
    # v3.0.17: chain-bump existing providers at this priority so the new
    # OAuth provider takes the slot it asked for.
    await _bump_priority_conflicts(db, data["priority"])

    provider = Provider(id=secrets.token_hex(8), **data)
    _stamp_user_edit(provider)
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    register_provider(
        provider.id, provider.provider_type,
        provider.hold_down_sec, provider.failure_threshold,
    )
    return _serialize(provider)


async def _do_rotate(
    spec: OAuthProviderSpec,
    provider_id: str,
    body: OAuthRotateRequest,
    db: AsyncSession,
) -> dict:
    """Exchange the callback code for tokens and UPDATE an existing
    Provider row in-place. v2.7.7 — used when tokens are server-side
    revoked or the refresh chain breaks; admin re-auths via the UI's
    "Re-auth" button."""
    flow = _flow_module(spec)
    from app.api.providers import _get_or_404, _stamp_user_edit, _serialize

    p = await _get_or_404(db, provider_id)
    if p.provider_type != spec.provider_type:
        raise HTTPException(
            400,
            f"Provider {p.name!r} is not a {spec.provider_type} provider",
        )

    try:
        code, callback_state = flow.extract_code_from_callback(body.callback)
    except ValueError as e:
        raise HTTPException(400, f"Couldn't parse callback: {e}")
    try:
        result = await flow.exchange_code(
            body.state, code, expected_state=callback_state,
        )
    except flow.OAuthFlowError as e:
        raise HTTPException(400, str(e))

    p.api_key = result.access_token
    p.oauth_refresh_token = result.refresh_token
    p.oauth_expires_at = result.expires_at

    if spec.extra_config_keys:
        cfg = dict(p.extra_config or {})
        for key in spec.extra_config_keys:
            value = getattr(result, key, None)
            if value:
                cfg[key] = value
        p.extra_config = cfg

    _stamp_user_edit(p)
    await db.commit()
    await db.refresh(p)
    register_provider(
        p.id, p.provider_type, p.hold_down_sec, p.failure_threshold,
    )
    # v2.7.8 BUG-002: fresh tokens — clear stale auth-failure + close breaker
    from app.routing.circuit_breaker import (
        clear_auth_failure as _clear_af, force_close,
    )
    _clear_af(p.id)
    await force_close(p.id)
    return _serialize(p)


# ── Endpoints — claude-oauth (Claude Pro Max) ────────────────────────────────


@router.post("/claude-oauth/authorize", response_model=OAuthAuthorizeResponse)
async def claude_oauth_authorize(
    _: AdminUser = Depends(require_admin),
):
    """Start a Claude Pro Max OAuth flow. Returns the URL the admin opens
    in another tab to approve on claude.ai."""
    return _do_authorize(CLAUDE_OAUTH_SPEC)


@router.post("/claude-oauth/exchange")
async def claude_oauth_exchange(
    body: OAuthExchangeRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Exchange the callback code for tokens and create the claude-oauth
    Provider row in one shot."""
    return await _do_exchange_create(CLAUDE_OAUTH_SPEC, body, db)


@router.post("/{provider_id}/oauth-rotate")
async def claude_oauth_rotate(
    provider_id: str,
    body: OAuthRotateRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Re-auth an existing claude-oauth provider in-place (v2.7.7).

    Used when tokens have been revoked server-side or the refresh chain
    breaks — admin completes the browser flow and pastes back the
    CODE#STATE; we update the existing Provider row without creating a
    duplicate.
    """
    return await _do_rotate(CLAUDE_OAUTH_SPEC, provider_id, body, db)


# ── Endpoints — codex-oauth (OpenAI Codex CLI / ChatGPT subscription) ────────
# Mirrors the claude-oauth flow. The browser redirect_uri is
# ``http://localhost:1455/auth/callback`` (Codex CLI's pre-registered
# callback at OpenAI), so the admin's browser will dead-end at that
# unreachable URL — they paste the URL back here and we extract the code.


@router.post("/codex-oauth/authorize", response_model=OAuthAuthorizeResponse)
async def codex_oauth_authorize(
    _: AdminUser = Depends(require_admin),
):
    """Start an OpenAI Codex / ChatGPT subscription OAuth flow.

    Returns the URL the admin opens in their browser. After approving,
    OpenAI redirects to ``http://localhost:1455/auth/callback?code=…&state=…``
    which won't load (the CLI's local listener isn't running on the admin's
    machine), but the URL bar carries the code. The admin pastes that URL
    back into ``/codex-oauth/exchange``.
    """
    return _do_authorize(CODEX_OAUTH_SPEC)


@router.post("/codex-oauth/exchange")
async def codex_oauth_exchange(
    body: OAuthExchangeRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Exchange the callback code for tokens and create the codex-oauth
    Provider row in one shot."""
    return await _do_exchange_create(CODEX_OAUTH_SPEC, body, db)


@router.post("/{provider_id}/codex-oauth-rotate")
async def codex_oauth_rotate(
    provider_id: str,
    body: OAuthRotateRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Re-auth an existing codex-oauth provider in-place. Mirrors the
    claude-oauth ``/oauth-rotate`` endpoint — separate URL so the UI can
    call the right exchange + know the right provider type."""
    return await _do_rotate(CODEX_OAUTH_SPEC, provider_id, body, db)


# ── Cursor (v4.4.31) ─────────────────────────────────────────────────────────
#
# Cursor's deep-link login differs from Anthropic/OpenAI PKCE: instead of
# a redirect carrying ``?code=…&state=…``, the operator pastes the
# ``WorkosCursorSessionToken`` cookie from cursor.com DevTools. The
# exchange handler POSTs that cookie to the Cursor-To-OpenAI sidecar's
# ``/cursor/loginDeepControl`` endpoint, which converts to the
# long-lived ``user_<id>::<JWT>`` access cookie consumed by chat
# dispatch. See app/providers/cursor_oauth_flow.py for the dance and
# docs/cursor-oauth-onboarding.md for the operator-facing flow.


@router.post("/cursor-oauth/authorize", response_model=OAuthAuthorizeResponse)
async def cursor_oauth_authorize(
    _: AdminUser = Depends(require_admin),
):
    """Start a Cursor subscription onboarding flow.

    Returns the URL the admin opens in their browser
    (``https://www.cursor.com/dashboard``). Cursor either lands them
    there directly (already signed in) or prompts a sign-in first;
    either way a fresh ``WorkosCursorSessionToken`` cookie ends up
    in the browser's cookie jar for ``cursor.com``.
    """
    return _do_authorize(CURSOR_OAUTH_SPEC)


@router.post("/cursor-oauth/exchange")
async def cursor_oauth_exchange(
    body: OAuthExchangeRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Convert the pasted ``WorkosCursorSessionToken`` cookie into the
    long-lived Cursor access cookie via the sidecar, then create the
    cursor-oauth Provider row in one shot. ``base_url`` is pinned to
    the sidecar inside ``_do_exchange_create``."""
    return await _do_exchange_create(CURSOR_OAUTH_SPEC, body, db)


@router.post("/{provider_id}/cursor-oauth-rotate")
async def cursor_oauth_rotate(
    provider_id: str,
    body: OAuthRotateRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Re-auth an existing cursor-oauth provider in-place. Cursor JWTs
    have a multi-week lifetime; when they expire the operator pastes a
    fresh ``WorkosCursorSessionToken`` here to mint a new access
    cookie without losing the Provider's id / priority / config."""
    return await _do_rotate(CURSOR_OAUTH_SPEC, provider_id, body, db)
