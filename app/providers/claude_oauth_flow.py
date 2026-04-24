"""
Interactive OAuth Authorization-Code flow for Claude Pro Max (v2.7.1).

Anthropic publishes a public OAuth client-metadata JSON at
``https://claude.ai/oauth/claude-code-client-metadata`` that declares
Claude Code as an RFC 7591 dynamically-registered public client:

    client_id (URL):    https://claude.ai/oauth/claude-code-client-metadata
    authorize endpoint: https://platform.claude.com/oauth/authorize
    token endpoint:     https://platform.claude.com/v1/oauth/token
    redirect_uris:      http://localhost/callback, http://127.0.0.1/callback
    auth method:        none  (public client, no client_secret; PKCE S256)
    grant_types:        authorization_code, refresh_token

Because the only whitelisted redirect URIs are localhost, we can't host
the redirect ourselves. Instead:
    1. Admin clicks "Generate Auth URL" — our backend builds the PKCE
       authorize URL with ``redirect_uri=http://localhost/callback`` and
       stores the ``code_verifier`` keyed on a random ``state``.
    2. Admin opens the URL in their browser, approves on claude.ai, and
       gets redirected to ``http://localhost/callback?code=XXX&state=YYY``.
       That URL fails to load (nothing on localhost), but the admin can
       copy it from the address bar.
    3. Admin pastes the full URL (or just ``code=XXX``) back into the UI.
       Our backend looks up the state, exchanges the code + verifier for
       tokens, and returns the access_token / refresh_token / expires_at
       to wire into a new Provider row.

Scope list observed from Claude Code's binary: ``user:profile
user:inference user:sessions:claude_code user:mcp_servers``.

Pending-state store is in-memory; a pending flow is dropped after
``PENDING_TTL_SEC`` (default 10 min) or after a successful exchange.
If the process restarts mid-flow, the admin just clicks "Generate
Auth URL" again.
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
AUTHORIZE_URL = "https://platform.claude.com/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
REDIRECT_URI = "http://localhost/callback"
DEFAULT_SCOPE = "user:profile user:inference user:sessions:claude_code user:mcp_servers"

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
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return AuthorizeStart(state=state, authorize_url=f"{AUTHORIZE_URL}?{urlencode(params)}")


def extract_code_from_callback(raw: str) -> tuple[str, Optional[str]]:
    """Accept either:
       - the full callback URL (``http://localhost/callback?code=XXX&state=YYY``)
       - a bare ``code=XXX&state=YYY`` query fragment
       - just the code value (``XXX``)

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

    form = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": pending.code_verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.post(TOKEN_URL, data=form)
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
    """Use a stored refresh_token to mint a new access_token."""
    form = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.post(TOKEN_URL, data=form)
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
