"""
Interactive OAuth Authorization-Code flow for Claude Pro Max (v2.7.1).

Claude Code's CLI uses an OAuth 2.0 authorization-code + PKCE flow against
a pre-registered public client. Values extracted from the
``@anthropic-ai/claude-code`` binary (v2.1.119):

    client_id:          9d1c250a-e61b-44d9-88ed-5944d1962f5e
    authorize endpoint: https://claude.com/cai/oauth/authorize
                        (redirects to https://claude.ai/oauth/authorize)
    token endpoint:     https://platform.claude.com/v1/oauth/token
    redirect_uri:       https://platform.claude.com/oauth/code/callback
    auth method:        none  (public client, no client_secret; PKCE S256)
    grant_types:        authorization_code, refresh_token

The callback URL is a real Anthropic-hosted page that, after the user
approves on claude.ai, displays the authorization code for copy-paste
(this is how CC's ``claude /login`` web mode works when no local
callback listener is available). That's exactly the UX we want:

    1. Admin clicks "Generate Auth URL" — our backend builds the PKCE
       authorize URL with ``redirect_uri=https://platform.claude.com/oauth/code/callback``
       and stores the ``code_verifier`` keyed on a random ``state``.
    2. Admin opens the URL in a browser where they're signed in to
       claude.ai, approves access, and is redirected to Anthropic's
       success page which displays the code.
    3. Admin copies the code (or the full callback URL) and pastes it
       back into the UI. Our backend matches the state, exchanges code +
       verifier for tokens, and returns access/refresh/expires_at to
       wire into a new Provider row.

Pending-state store is in-memory; a pending flow is dropped after
``PENDING_TTL_SEC`` (default 10 min) or after a successful exchange.
If the process restarts mid-flow, the admin just clicks "Generate
Auth URL" again.

Historical note: v2.7.1's first draft used the RFC 7591 dynamic-client
metadata URL (``https://claude.ai/oauth/claude-code-client-metadata``)
as the client_id and ``http://localhost/callback`` as the redirect_uri.
That combination **was not accepted** by claude.ai's SSO gateway —
users got a generic "error logging you in" page after approving.
Switching to the pre-registered UUID + platform.claude.com redirect
(the same pair the CLI uses) is what actually works.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx


# ── Endpoints (extracted from the @anthropic-ai/claude-code binary, v2.1.119) ─
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
DEFAULT_SCOPE = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

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
    """RFC 7636: 43-128 chars of [A-Za-z0-9-._~]."""
    return secrets.token_urlsafe(length)[:length]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


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
        # ``code=true`` is the extra flag Anthropic's authorize endpoint
        # requires to display the code on the redirect page. Without it,
        # the flow still completes but the success page doesn't surface the
        # code for copy-paste.
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    return AuthorizeStart(state=state, authorize_url=f"{AUTHORIZE_URL}?{urlencode(params)}")


def extract_code_from_callback(raw: str) -> tuple[str, Optional[str]]:
    """Accept any of the paste formats Anthropic's success page surfaces:

    - ``CODE#STATE`` — the single-token format CC's success page shows in
      a "Copy" button (this is the dominant case in practice).
    - Full callback URL:
      ``https://platform.claude.com/oauth/code/callback?code=XXX&state=YYY``
    - A bare ``code=XXX&state=YYY`` query fragment.
    - Just the code value (no state) — only useful when the caller already
      knows the state out-of-band.

    Returns ``(code, state_or_None)``.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty callback")
    # Full URL
    if raw.startswith("http://") or raw.startswith("https://"):
        q = parse_qs(urlparse(raw).query)
        code = (q.get("code") or [None])[0]
        state = (q.get("state") or [None])[0]
        if not code:
            raise ValueError("URL has no `code` query parameter")
        return code, state
    # Query fragment
    if "=" in raw:
        q = parse_qs(raw.lstrip("?"))
        code = (q.get("code") or [None])[0]
        state = (q.get("state") or [None])[0]
        if not code:
            raise ValueError("No `code` parameter found")
        return code, state
    # CODE#STATE (the single-token copy-paste format)
    if "#" in raw:
        code, state = raw.split("#", 1)
        if not code or not state:
            raise ValueError(
                "Code looks truncated — expected format 'CODE#STATE'. "
                "Please copy the full string from the success page."
            )
        return code, state
    # Bare code
    return raw, None


@dataclass
class ExchangeResult:
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[float]  # unix timestamp
    raw: dict  # the full token response for debugging


class OAuthFlowError(Exception):
    pass


async def exchange_code(
    state: str, code: str, *, expected_state: Optional[str] = None,
) -> ExchangeResult:
    """Trade the authorization code for access + refresh tokens.

    ``state`` must match a flow started with ``start_authorize``. When
    the caller also has a state value parsed from the callback URL, they
    can pass it as ``expected_state`` for a double-check — we reject if
    the two don't match (defense in depth, in case an admin pastes a
    callback from the wrong flow).
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

    # Note: Anthropic's /v1/oauth/token requires ``state`` in the form —
    # non-standard for OAuth2, but the CC CLI sends it and the server 400s
    # without it. POST as JSON (CC uses application/json, not form-urlencoded).
    form = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": pending.code_verifier,
        "state": state,
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.post(TOKEN_URL, json=form)
    if resp.status_code >= 400:
        # Put the pending back so the admin can retry with a fresh code
        # without re-clicking the authorize URL (Anthropic usually allows
        # the same auth code to be exchanged within a short window).
        _PENDING[state] = pending
        raise OAuthFlowError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:400]}"
        )
    data = resp.json()
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthFlowError(f"Upstream returned no access_token: {data}")
    refresh = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else None
    expires_at = None
    if "expires_in" in data:
        try:
            expires_at = time.time() + float(data["expires_in"])
        except (TypeError, ValueError):
            expires_at = None
    return ExchangeResult(
        access_token=access, refresh_token=refresh, expires_at=expires_at, raw=data,
    )


async def refresh_access_token(refresh_token: str) -> ExchangeResult:
    """Low-level token refresh — DO NOT call from production code paths.

    Use ``refresh_and_persist(provider, db)`` instead. Anthropic rotates
    refresh tokens on use, so the returned ``ExchangeResult.refresh_token``
    is different from the one passed in and MUST be persisted to the
    Provider row. Calling this directly without persisting drops the
    rotated token and the next refresh fails with ``invalid_grant``.

    This function exists for unit tests and the one-shot exchange in
    ``refresh_and_persist`` itself.
    """
    form = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.post(TOKEN_URL, json=form)
    if resp.status_code >= 400:
        raise OAuthFlowError(
            f"Refresh failed ({resp.status_code}): {resp.text[:400]}"
        )
    data = resp.json()
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthFlowError(f"Refresh returned no access_token: {data}")
    new_refresh = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else refresh_token
    expires_at = None
    if "expires_in" in data:
        try:
            expires_at = time.time() + float(data["expires_in"])
        except (TypeError, ValueError):
            expires_at = None
    return ExchangeResult(
        access_token=access, refresh_token=new_refresh, expires_at=expires_at, raw=data,
    )


async def refresh_and_persist(provider, db) -> ExchangeResult:
    """Refresh a claude-oauth Provider's access_token and write the rotated
    refresh_token + new expiry back to the DB in the same transaction.

    This is what production code paths (messages dispatch, scanner) should
    call when they see a 401 Unauthorized from platform.claude.com — never
    ``refresh_access_token()`` directly, because that drops the rotated
    token on the floor.
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
    await db.commit()
    return result
