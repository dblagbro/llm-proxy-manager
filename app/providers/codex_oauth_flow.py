"""
Interactive OAuth Authorization-Code flow for OpenAI Codex CLI / ChatGPT
subscription (v3.0.15).

Codex CLI uses an OAuth 2.0 authorization-code + PKCE flow against a
pre-registered public client at ``auth.openai.com``. Values extracted
from the open-source Codex CLI (github.com/openai/codex, HEAD ~rust-v0.128.0):

    client_id:          app_EMoamEEZ73f0CkXaXp7hrann
    authorize endpoint: https://auth.openai.com/oauth/authorize
    token endpoint:     https://auth.openai.com/oauth/token  (POST JSON)
    redirect_uri:       http://localhost:1455/auth/callback
    auth method:        none  (public client, no client_secret; PKCE S256)
    grant_types:        authorization_code, refresh_token
    scopes:             openid profile email offline_access
                        api.connectors.read api.connectors.invoke

UX, mirroring the claude-oauth flow:

    1. Admin clicks "Generate Auth URL" — backend builds the PKCE
       authorize URL with ``redirect_uri=http://localhost:1455/auth/callback``
       and stores the ``code_verifier`` keyed on a random ``state``.
    2. Admin opens the URL in a browser where they're signed in to
       ChatGPT (Plus/Team/Enterprise), approves access, and is redirected
       to ``http://localhost:1455/auth/callback?code=...&state=...``.
       The browser shows "site can't be reached" since nothing is
       listening on 1455 on the admin's workstation; the address bar
       still has the full callback URL.
    3. Admin copies the URL from the address bar (or just the code+state
       query fragment) and pastes it back into the UI. We extract the
       code, match the state, exchange the code for tokens, and return
       access/refresh/expires_at + the JWT id_token (which carries
       ChatGPT-Account-ID and the plan tier as custom claims).

Pending-state store is in-memory; a pending flow is dropped after
``PENDING_TTL_SEC`` (default 10 min) or after a successful exchange.

The id_token JWT carries:
  - ``https://api.openai.com/auth.chatgpt_account_id`` — workspace id
    that must be sent on every chat call as ``ChatGPT-Account-ID``.
  - ``https://api.openai.com/auth.chatgpt_plan_type`` — tier label.
We parse these on exchange and surface them so the provider row can
store them next to the tokens.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx


# ── Endpoints (extracted from github.com/openai/codex source) ───────────────
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = (
    "openid profile email offline_access "
    "api.connectors.read api.connectors.invoke"
)

# Extra non-scope query params the Codex CLI passes; mirroring them
# minimizes the chance that the OpenAI consent UX behaves differently
# from the CLI flow.
_EXTRA_AUTH_PARAMS = {
    "id_token_add_organizations": "true",
    "codex_cli_simplified_flow": "true",
    "originator": "codex_cli_rs",
}

PENDING_TTL_SEC = 600


# ── Pending-flow state (in-memory) ──────────────────────────────────────────
@dataclass
class _PendingFlow:
    code_verifier: str
    created_at: float


_PENDING: dict[str, _PendingFlow] = {}


def _sweep_pending(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    stale = [k for k, v in _PENDING.items() if now - v.created_at > PENDING_TTL_SEC]
    for k in stale:
        _PENDING.pop(k, None)


# ── PKCE helpers ────────────────────────────────────────────────────────────
def _gen_code_verifier(length: int = 64) -> str:
    """RFC 7636: 43-128 chars of [A-Za-z0-9-._~]. Codex uses 64 random bytes
    base64url-encoded → ~86 chars."""
    return secrets.token_urlsafe(length)[:length]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── id_token JWT parsing ────────────────────────────────────────────────────
# OpenAI nests its custom claims under a single namespace key whose value
# is an object, not multiple flat keys with URI-shaped names:
#   "https://api.openai.com/auth": {
#     "chatgpt_account_id": "...",
#     "chatgpt_plan_type": "plus",
#     "chatgpt_subscription_active_start": "...",
#     "chatgpt_subscription_active_until": "...",
#   }
_AUTH_NAMESPACE = "https://api.openai.com/auth"


def _decode_jwt_payload(jwt: str) -> dict:
    """Parse the JWT payload without verifying signature.

    We only use the claims for routing/header-population — the access_token
    is the actual auth credential. Trusting the id_token contents is fine
    here because OpenAI itself returned them via TLS to us.
    """
    parts = jwt.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)  # pad base64
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def parse_id_token(jwt: str) -> tuple[Optional[str], Optional[str]]:
    """Returns ``(chatgpt_account_id, chatgpt_plan_type)`` from the id_token,
    or ``(None, None)`` on parse failure."""
    payload = _decode_jwt_payload(jwt)
    auth = payload.get(_AUTH_NAMESPACE)
    if not isinstance(auth, dict):
        return (None, None)
    return (
        auth.get("chatgpt_account_id"),
        auth.get("chatgpt_plan_type"),
    )


# ── Public API ──────────────────────────────────────────────────────────────
@dataclass
class AuthorizeStart:
    state: str
    authorize_url: str


def start_authorize(scope: str = DEFAULT_SCOPE) -> AuthorizeStart:
    """Generate a fresh state + PKCE pair and return the URL the admin clicks."""
    _sweep_pending()
    state = secrets.token_urlsafe(24)
    verifier = _gen_code_verifier()
    _PENDING[state] = _PendingFlow(code_verifier=verifier, created_at=time.time())
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
        **_EXTRA_AUTH_PARAMS,
    }
    return AuthorizeStart(state=state, authorize_url=f"{AUTHORIZE_URL}?{urlencode(params)}")


def extract_code_from_callback(raw: str) -> tuple[str, Optional[str]]:
    """Accept any of the paste formats from the Codex callback page:

    - Full callback URL: ``http://localhost:1455/auth/callback?code=XXX&state=YYY``
    - Bare query fragment: ``code=XXX&state=YYY``
    - Just the code value (no state) — only useful when the caller already
      knows the state out-of-band.

    Returns ``(code, state_or_None)``.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty callback")
    if raw.startswith("http://") or raw.startswith("https://"):
        q = parse_qs(urlparse(raw).query)
        code = (q.get("code") or [None])[0]
        state = (q.get("state") or [None])[0]
        if not code:
            raise ValueError("URL has no `code` query parameter")
        return code, state
    if "=" in raw:
        q = parse_qs(raw.lstrip("?"))
        code = (q.get("code") or [None])[0]
        state = (q.get("state") or [None])[0]
        if not code:
            raise ValueError("No `code` parameter found")
        return code, state
    return raw, None


@dataclass
class ExchangeResult:
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[float]   # unix timestamp
    id_token: Optional[str]
    chatgpt_account_id: Optional[str]
    chatgpt_plan_type: Optional[str]
    raw: dict                     # full token response for debugging


class OAuthFlowError(Exception):
    pass


def _result_from_token_response(data: dict, fallback_refresh: Optional[str] = None) -> ExchangeResult:
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthFlowError(f"Upstream returned no access_token: {data}")
    refresh = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else fallback_refresh
    id_token = data.get("id_token") if isinstance(data.get("id_token"), str) else None
    expires_at = None
    if "expires_in" in data:
        try:
            expires_at = time.time() + float(data["expires_in"])
        except (TypeError, ValueError):
            expires_at = None
    account_id = plan_type = None
    if id_token:
        account_id, plan_type = parse_id_token(id_token)
    return ExchangeResult(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        id_token=id_token,
        chatgpt_account_id=account_id,
        chatgpt_plan_type=plan_type,
        raw=data,
    )


async def exchange_code(
    state: str, code: str, *, expected_state: Optional[str] = None,
) -> ExchangeResult:
    """Trade the authorization code for access + refresh tokens.

    ``state`` must match a flow started with ``start_authorize``. When
    the caller also has a state value parsed from the callback URL, they
    can pass it as ``expected_state`` for a double-check.
    """
    _sweep_pending()
    if expected_state is not None and expected_state != state:
        raise OAuthFlowError(
            "state mismatch — the callback URL's state doesn't match the pending flow"
        )
    pending = _PENDING.pop(state, None)
    if pending is None:
        raise OAuthFlowError(
            "Unknown or expired state. Click 'Generate Auth URL' again — the "
            "flow expires after 10 minutes."
        )

    body = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": pending.code_verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.post(TOKEN_URL, json=body)
    if resp.status_code >= 400:
        # Put the pending back so admin can retry without re-authorizing
        _PENDING[state] = pending
        raise OAuthFlowError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:400]}"
        )
    return _result_from_token_response(resp.json())


async def refresh_access_token(refresh_token: str) -> ExchangeResult:
    """Low-level token refresh — DO NOT call from production code paths.

    Use ``refresh_and_persist(provider, db)`` instead. OpenAI rotates
    refresh tokens on use; the returned ``refresh_token`` is different
    from the one passed in and MUST be persisted to the Provider row.
    """
    body = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.post(TOKEN_URL, json=body)
    if resp.status_code >= 400:
        raise OAuthFlowError(
            f"Refresh failed ({resp.status_code}): {resp.text[:400]}"
        )
    return _result_from_token_response(resp.json(), fallback_refresh=refresh_token)


# v5.8.5 — per-provider single-flight lock around refresh_and_persist.
# Same race condition as v5.8.3 fixed for claude_oauth_flow: the proactive
# expiry sweep (cursor_oauth_expiry_monitor) and the lazy-on-401 path
# (scanner._fetch_codex_oauth_models, dispatch) can both call this for the
# same provider concurrently. OpenAI rotates refresh_token on every use,
# so whichever caller loses the race gets ``refresh_token_reused``. The
# 2026-06-20 smoke incident (b9db96fad980bae1 + bd42da809fd26ffd both
# tripping ``refresh_token_reused`` within the same sweep) is this.
# Lock is process-local; cluster-level races still fall back to v3.0.18
# peer-pull.
_refresh_locks: dict[str, "asyncio.Lock"] = {}


def _get_refresh_lock(provider_id: str) -> "asyncio.Lock":
    import asyncio
    lock = _refresh_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[provider_id] = lock
    return lock


async def refresh_and_persist(provider, db) -> ExchangeResult:
    """Refresh a codex-oauth Provider's access_token and write the rotated
    refresh_token + new expiry back to the DB in the same transaction.

    Production paths must call THIS helper, never ``refresh_access_token``
    directly, because OpenAI rotates the refresh_token and dropping the
    rotated value bricks the next refresh.

    v5.8.5 (mirrors v5.8.3 for claude-oauth):
      - single-flight per-provider lock (intra-process race fix that
        prevented ``refresh_token_reused`` storms from the proactive
        sweep colliding with lazy-on-401 refresh).
      - on adopted peer state, verify the adopted token actually
        authenticates against chatgpt.com before keeping it; if it 401s,
        clear oauth_refresh_token and raise so the operator sees
        ``Needs re-auth`` instead of a silent cluster-wide dead-token.
    """
    if not provider.oauth_refresh_token:
        raise OAuthFlowError(
            f"Provider {provider.id} ({provider.name!r}) has no refresh_token — "
            "admin must re-run the Generate Auth URL flow."
        )
    lock = _get_refresh_lock(provider.id)
    async with lock:
        # v5.8.5 — re-read inside the lock so a waiter doesn't burn the
        # already-rotated token.
        await db.refresh(provider, attribute_names=["oauth_refresh_token", "api_key", "oauth_expires_at"])
        if not provider.oauth_refresh_token:
            raise OAuthFlowError(
                f"Provider {provider.id} ({provider.name!r}) lost its "
                "refresh_token mid-refresh (likely v5.8.5 verify-after-adopt "
                "cleared it). Re-run the Generate Auth URL flow."
            )
        try:
            result = await refresh_access_token(provider.oauth_refresh_token)
        except OAuthFlowError as e:
            # v3.0.18: same recovery as claude-oauth — on invalid_grant, ask
            # peers if any of them refreshed first and adopt their fresh state.
            msg = str(e).lower()
            if "invalid_grant" in msg or "refresh_token_expired" in msg or "refresh_token_reused" in msg or "refresh_token_invalidated" in msg:
                from app.cluster.oauth_recovery import (
                    pull_oauth_state_from_peers, adopt_peer_state,
                )
                peer_state = await pull_oauth_state_from_peers(provider.id)
                if peer_state is not None:
                    await adopt_peer_state(provider, db, peer_state)
                    # v5.8.5 — verify the adopted token authenticates before
                    # we declare success. A peer that lost the same race has
                    # the same dead state we just tried.
                    cfg = peer_state.extra_config or {}
                    verified = await _verify_oauth_access_token(
                        peer_state.api_key,
                        chatgpt_account_id=cfg.get("chatgpt_account_id"),
                    )
                    if verified:
                        return ExchangeResult(
                            access_token=peer_state.api_key,
                            refresh_token=peer_state.oauth_refresh_token,
                            expires_at=peer_state.oauth_expires_at,
                            id_token=None,
                            chatgpt_account_id=cfg.get("chatgpt_account_id"),
                            chatgpt_plan_type=cfg.get("chatgpt_plan_type"),
                            raw={"recovered_from_peer": peer_state.source_peer_id},
                        )
                    # Adopted state is also dead — clear refresh_token so
                    # the operator gets a hard "Needs re-auth" signal.
                    provider.oauth_refresh_token = None
                    provider.api_key = peer_state.api_key  # keep for audit visibility
                    await db.commit()
                    raise OAuthFlowError(
                        f"Provider {provider.id} ({provider.name!r}) refresh "
                        "failed AND the peer-adopted state also failed to "
                        "authenticate against chatgpt.com. Refresh token "
                        "appears permanently revoked. Operator must re-auth "
                        "via Providers page → Re-authorize."
                    ) from e
            raise
        provider.api_key = result.access_token
        if result.refresh_token:
            provider.oauth_refresh_token = result.refresh_token
        provider.oauth_expires_at = result.expires_at
        # Account id is also stored on the row (in extra_config) so dispatch
        # can stamp the ChatGPT-Account-ID header without re-decoding the JWT
        # on every call.
        if result.chatgpt_account_id and provider.extra_config is not None:
            cfg = dict(provider.extra_config)
            cfg["chatgpt_account_id"] = result.chatgpt_account_id
            if result.chatgpt_plan_type:
                cfg["chatgpt_plan_type"] = result.chatgpt_plan_type
            provider.extra_config = cfg
        await db.commit()
        return result


async def _verify_oauth_access_token(token: str, chatgpt_account_id: Optional[str] = None) -> bool:
    """v5.8.5 — cheap probe that confirms ``token`` actually authenticates
    against chatgpt.com. Used after peer-adopt to make sure we're not
    committing a token the rest of the cluster has already lost. Any
    non-401 response (including 429 rate-limit) counts as authenticated.
    Returns ``False`` only when chatgpt.com explicitly rejects the token.

    Mirrors ``_fetch_codex_oauth_models``'s request shape but never
    raises: this is a verdict-only helper.
    """
    try:
        from app.providers.codex_oauth import (
            CODEX_MODELS_URL, CODEX_CLIENT_VERSION, build_headers,
        )
    except Exception:
        return True  # can't construct the request; don't punish the token
    try:
        async with httpx.AsyncClient(timeout=8.0) as cli:
            resp = await cli.get(
                f"{CODEX_MODELS_URL}?client_version={CODEX_CLIENT_VERSION}",
                headers=build_headers(
                    token,
                    chatgpt_account_id=chatgpt_account_id,
                    extra={"Accept": "application/json"},
                ),
            )
        return resp.status_code != 401
    except Exception:
        # Network errors don't count as a failed-auth verdict; only an
        # explicit 401 from chatgpt.com does.
        return True
