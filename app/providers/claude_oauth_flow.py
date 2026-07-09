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


async def _internal_refresh_access_token(refresh_token: str) -> ExchangeResult:
    """v3.9.15 (BUG-007) — internal-only token refresh.

    Renamed from ``refresh_access_token`` to discourage direct discovery
    via autocomplete / casual imports. The old name remains as a thin
    alias below for one release for callers (only the burn-test live
    script today) that pin it.

    DO NOT call from production code paths. Anthropic rotates refresh
    tokens on use, so ``ExchangeResult.refresh_token`` is different from
    the input and MUST be persisted to the Provider row. Calling this
    without persisting drops the rotated token; next refresh fails with
    ``invalid_grant``.

    Production-safe wrapper: ``refresh_and_persist(provider, db)``.
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


# v3.9.15 (BUG-007) — back-compat alias for callers that pinned the old
# name. Emits a DeprecationWarning on import-use so they migrate. Will
# be removed in v3.10.x. The single known caller today is
# ``scripts/test_claude_oauth_live.py`` (the destructive burn test),
# which is migrated in the same release.
async def refresh_access_token(refresh_token: str) -> ExchangeResult:
    import warnings
    warnings.warn(
        "refresh_access_token is deprecated; this helper is internal-only. "
        "Use refresh_and_persist(provider, db) for production code paths, "
        "or _internal_refresh_access_token() for tests.",
        DeprecationWarning,
        stacklevel=2,
    )
    return await _internal_refresh_access_token(refresh_token)


# v5.8.3 — per-provider single-flight lock. Pre-5.8.3 the proactive
# expiry sweep + the lazy-on-401 refresh path could both call this
# function for the same provider within milliseconds. Anthropic
# rotates the refresh_token on every successful use, so whichever
# caller LOST the race got 400 ``invalid_grant: Refresh token not
# found or invalid`` (Anthropic has already revoked the now-stale
# refresh_token it gave the winner). The cluster-fallback path then
# pulled stale tokens from peers (who lost the same race), surfacing
# as steady 401 storms on keepalive probes. The 2026-06-20 incident
# (1369+1363 errors in 24h on Devin-Anthropic-Max-{Gmail,VG}) was
# this. Lock is process-local; cluster-level races between nodes
# still use the v3.0.18 peer-pull path.
_refresh_locks: dict[str, "asyncio.Lock"] = {}


def _get_refresh_lock(provider_id: str) -> "asyncio.Lock":
    import asyncio
    lock = _refresh_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[provider_id] = lock
    return lock


async def refresh_and_persist(provider, db) -> ExchangeResult:
    """Refresh a claude-oauth Provider's access_token and write the rotated
    refresh_token + new expiry back to the DB in the same transaction.

    This is what production code paths (messages dispatch, scanner) should
    call when they see a 401 Unauthorized from platform.claude.com — never
    ``refresh_access_token()`` directly, because that drops the rotated
    token on the floor.

    v3.0.18: on ``invalid_grant`` (peer rotated the refresh_token first in
    a cluster-race), fan out to peers via /cluster/oauth-pull and adopt
    the freshest valid state. Only raises if no peer has fresher tokens.

    v5.8.3:
      - single-flight per-provider lock (intra-process race fix)
      - on adopted peer state, verify the adopted token actually
        authenticates against Anthropic before keeping it; if it 401s,
        clear oauth_refresh_token and raise so the operator sees
        ``Needs re-auth`` in the UI instead of a silent cluster-wide
        dead-token state.
    """
    if not provider.oauth_refresh_token:
        raise OAuthFlowError(
            f"Provider {provider.id} ({provider.name!r}) has no refresh_token — "
            "admin must re-run the Generate Auth URL flow."
        )
    lock = _get_refresh_lock(provider.id)
    async with lock:
        # v5.8.3 — re-read the row inside the lock. If another waiter just
        # rotated the token, our pre-lock copy of refresh_token is now
        # stale; the value just persisted is in the DB.
        await db.refresh(provider, attribute_names=["oauth_refresh_token", "api_key", "oauth_expires_at"])
        if not provider.oauth_refresh_token:
            raise OAuthFlowError(
                f"Provider {provider.id} ({provider.name!r}) lost its "
                "refresh_token mid-refresh (likely v5.8.3 verify-after-adopt "
                "cleared it). Re-run the Generate Auth URL flow."
            )
        try:
            result = await refresh_access_token(provider.oauth_refresh_token)
        except OAuthFlowError as e:
            # v3.0.18 + v5.8.3: invalid_grant might mean a peer beat us
            # OR Anthropic permanently revoked the refresh_token. Try
            # peer-pull, then verify the adopted token before keeping it.
            if "invalid_grant" in str(e).lower():
                from app.cluster.oauth_recovery import (
                    pull_oauth_state_from_peers, adopt_peer_state,
                )
                peer_state = await pull_oauth_state_from_peers(provider.id)
                if peer_state is not None:
                    await adopt_peer_state(provider, db, peer_state)
                    # v5.8.3 — verify the adopted token authenticates.
                    # Use a tiny ping (1-token max) so it costs ~nothing.
                    verified = await _verify_oauth_access_token(peer_state.api_key)
                    if verified:
                        return ExchangeResult(
                            access_token=peer_state.api_key,
                            refresh_token=peer_state.oauth_refresh_token,
                            expires_at=peer_state.oauth_expires_at,
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
                        "authenticate against Anthropic. Refresh token "
                        "appears permanently revoked. Operator must re-auth "
                        "via Providers page → Re-authorize."
                    ) from e
            raise
        provider.api_key = result.access_token
        if result.refresh_token:
            provider.oauth_refresh_token = result.refresh_token
        provider.oauth_expires_at = result.expires_at
        await db.commit()
        return result


async def _verify_oauth_access_token(token: str) -> bool:
    """v5.8.3 — cheap probe that confirms ``token`` actually authenticates
    against platform.claude.com. Used after peer-adopt to make sure we're
    not committing a token the rest of the cluster has already lost. Any
    non-401 response (including 429 rate-limit) counts as authenticated.
    Returns ``False`` only when Anthropic explicitly rejects the token."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False) as cli:
            resp = await cli.post(
                "https://platform.claude.com/v1/messages?beta=true",
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "."}],
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                },
            )
        return resp.status_code != 401
    except Exception:
        # Network errors don't count as a failed-auth verdict; only
        # an explicit 401 from Anthropic does.
        return True
