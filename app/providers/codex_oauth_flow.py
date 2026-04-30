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


async def refresh_and_persist(provider, db) -> ExchangeResult:
    """Refresh a codex-oauth Provider's access_token and write the rotated
    refresh_token + new expiry back to the DB in the same transaction.

    Production paths must call THIS helper, never ``refresh_access_token``
    directly, because OpenAI rotates the refresh_token and dropping the
    rotated value bricks the next refresh.
    """
    if not provider.oauth_refresh_token:
        raise OAuthFlowError(
            f"Provider {provider.id} ({provider.name!r}) has no refresh_token — "
            "admin must re-run the Generate Auth URL flow."
        )
    result = await refresh_access_token(provider.oauth_refresh_token)
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
