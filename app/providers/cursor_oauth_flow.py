"""v4.4.31 — Cursor (Pro/Business subscription) OAuth-style onboarding.

Cursor doesn't expose a public PKCE OAuth like Anthropic or OpenAI. Its
IDE deep-link flow ends in a session cookie (`WorkosCursorSessionToken`)
which the IDE converts to a `user_<id>::<JWT>` access cookie via a
proprietary endpoint. We piggyback on the Cursor-To-OpenAI sidecar
(``llm-proxy2-cursor-bridge``) for the conversion + the chat dispatch
protocol — Cursor's wire format is ConnectRPC+protobuf with a
proprietary checksum, which the sidecar isolates from our codebase.

The onboarding contract here matches the shape of
``claude_oauth_flow`` and ``codex_oauth_flow`` (same function names +
return types) so ``providers_oauth.py`` can reuse the shared
``_do_authorize`` / ``_do_exchange_create`` / ``_do_rotate`` machinery
without per-vendor branching.

User-facing flow:

  1. Operator clicks "Generate Auth URL" → ``start_authorize()`` returns
     ``https://www.cursor.com/dashboard`` and a tracking ``state``. (Cursor
     redirects unauthenticated visitors to its sign-in page; once signed
     in, the dashboard lands a fresh ``WorkosCursorSessionToken`` cookie
     scoped to ``cursor.com``.)
  2. Operator opens the URL, signs in if needed, then copies the
     ``WorkosCursorSessionToken`` cookie value from DevTools → Application
     → Cookies → cursor.com.
  3. Operator pastes the cookie value back into the modal. We call the
     sidecar's ``/cursor/loginDeepControl`` endpoint with that cookie as
     a Bearer token; the sidecar exchanges it (via Cursor's internal
     auth API) for a long-lived ``user_<id>::<JWT>`` access cookie.
  4. We store the ``user_<id>::<JWT>`` cookie as the Provider's
     ``api_key`` — that's the credential the sidecar's chat dispatch
     consumes per request.

No ``code_verifier`` / ``code_challenge`` (Cursor isn't PKCE), no
``redirect_uri`` (Cursor uses deep-link polling, not a redirect), and
no refresh-token rotation for v1 (the JWT has a multi-week lifetime;
when it expires, the operator re-pastes via ``cursor-oauth-rotate``).
Plenty of polish runway in v2 — see ``docs/cursor-oauth-onboarding.md``.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Optional

import httpx


# Cursor's user-facing landing where the WorkosCursorSessionToken cookie
# gets minted. Sign-in is prompted if not already authenticated, and the
# resulting cookie lives on cursor.com (not the api2.cursor.sh subdomain
# the chat backend uses — the sidecar's /cursor/loginDeepControl bridges
# the two).
CURSOR_SIGNIN_URL = "https://www.cursor.com/dashboard"


# Where the sidecar exposes the WorkosCursorSessionToken → JWT
# conversion. Within the docker network this is reachable from
# llm-proxy2 by the service name. Configurable via env at runtime in
# case the sidecar is renamed or moved.
import os as _os
CURSOR_BRIDGE_URL = _os.environ.get(
    "CURSOR_BRIDGE_URL",
    "http://llm-proxy2-cursor-bridge:3010",
)


# Pending-state TTL. Cursor doesn't enforce a deadline on our side
# (state is just a random opaque token we mint), but expiring stale
# entries keeps the in-memory dict bounded if an operator abandons the
# flow.
_STATE_TTL_SEC = 600  # 10 min


@dataclass
class _PendingFlow:
    created_at: float


_PENDING: dict[str, _PendingFlow] = {}


def _sweep_pending(now: Optional[float] = None) -> None:
    """Drop pending-state entries older than ``_STATE_TTL_SEC``. Same
    pattern as codex_oauth_flow's sweep — the dict never grows unbounded
    even if the operator abandons flows."""
    cutoff = (now if now is not None else time.time()) - _STATE_TTL_SEC
    for state, flow in list(_PENDING.items()):
        if flow.created_at < cutoff:
            _PENDING.pop(state, None)


@dataclass
class AuthorizeStart:
    state: str
    authorize_url: str


def start_authorize(scope: Optional[str] = None) -> AuthorizeStart:
    """Mint a state token + return the URL the operator opens.

    ``scope`` is accepted but ignored — Cursor's session cookie scope is
    fixed. The parameter exists so the providers_oauth shared
    ``_do_authorize`` can call us with the same signature as claude /
    codex flows.
    """
    _sweep_pending()
    state = secrets.token_urlsafe(24)
    _PENDING[state] = _PendingFlow(created_at=time.time())
    return AuthorizeStart(state=state, authorize_url=CURSOR_SIGNIN_URL)


def extract_code_from_callback(raw: str) -> tuple[str, Optional[str]]:
    """Accept the WorkosCursorSessionToken cookie value the operator
    pastes back. The "callback" name is shared-shape with
    codex/claude flows; for Cursor it's just the cookie value (or, for
    convenience, a leading ``WorkosCursorSessionToken=`` / ``Cookie:``
    prefix we strip).

    Returns ``(token, None)`` — Cursor's flow doesn't carry our state
    back to us (cursor.com has no callback to our system), so the state
    is verified by the shared exchange-create handler from the body
    field directly.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Empty callback — paste the WorkosCursorSessionToken cookie value")

    # Tolerate convenience prefixes from copy/paste paths.
    for prefix in ("WorkosCursorSessionToken=", "Cookie: WorkosCursorSessionToken=", "Cookie:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
            break
    # Strip any trailing ``; <other-cookies>...`` if the operator copied
    # the whole Cookie header.
    if ";" in raw:
        raw = raw.split(";", 1)[0].strip()

    if not raw:
        raise ValueError("Cookie value is empty after prefix strip")

    # Sanity: WorkosCursorSessionToken values are URL-encoded JWTs that
    # always contain ``%3A%3A`` (the encoded ``::`` separator) — reject
    # obviously-wrong pastes here so the sidecar doesn't 500 later with
    # a less-readable error.
    if "%3A%3A" not in raw and "::" not in raw:
        raise ValueError(
            "Doesn't look like a WorkosCursorSessionToken — expected "
            "a URL-encoded JWT with ``%3A%3A`` (or ``::``) separator. "
            "Make sure you copied the cookie value, not the cookie name."
        )

    return raw, None


@dataclass
class ExchangeResult:
    """Mirrors codex_oauth_flow.ExchangeResult shape so the shared
    providers_oauth handler can stash fields uniformly. Cursor doesn't
    issue a refresh_token or expires_at via this flow — the JWT carries
    its own ``exp`` claim with a multi-week lifetime; rotation is manual.
    """
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[float]
    id_token: Optional[str]
    raw: dict


class OAuthFlowError(Exception):
    pass


async def exchange_code(
    state: str,
    code: str,
    *,
    expected_state: Optional[str] = None,
) -> ExchangeResult:
    """Convert the pasted WorkosCursorSessionToken into the long-lived
    ``user_<id>::<JWT>`` cookie via the sidecar's
    ``/cursor/loginDeepControl`` endpoint, then return it as
    ``access_token``.

    ``state`` is the value the operator's modal sent back; we verify it
    against the pending set so a stale modal can't replay an old
    authorize.
    """
    _sweep_pending()
    if expected_state and state != expected_state:
        raise OAuthFlowError("state mismatch")
    if state not in _PENDING:
        raise OAuthFlowError(
            "state not found (expired after 10 min, or never started "
            "via the Generate Auth URL button)"
        )
    _PENDING.pop(state, None)

    url = f"{CURSOR_BRIDGE_URL.rstrip('/')}/cursor/loginDeepControl"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {code}"})
    except httpx.HTTPError as e:
        # Network-level error talking to the sidecar — distinct from
        # Cursor rejecting the cookie (which the sidecar would 200 or 401).
        raise OAuthFlowError(
            f"Couldn't reach cursor-bridge sidecar at {CURSOR_BRIDGE_URL}: "
            f"{type(e).__name__}: {e}. Is llm-proxy2-cursor-bridge running?"
        ) from None

    if resp.status_code != 200:
        # Surface the upstream error verbatim (the sidecar passes through
        # Cursor's error text). The most common case here is the cookie
        # being expired or already invalidated.
        body_preview = resp.text[:300] if resp.text else "(empty body)"
        raise OAuthFlowError(
            f"cursor-bridge returned HTTP {resp.status_code} during "
            f"WorkosCursorSessionToken exchange: {body_preview}"
        )

    try:
        data = resp.json()
    except Exception:
        raise OAuthFlowError(
            f"cursor-bridge returned non-JSON: {resp.text[:300]}"
        ) from None

    access = data.get("accessToken")
    if not isinstance(access, str) or not access:
        raise OAuthFlowError(
            f"cursor-bridge response missing accessToken: keys={sorted(data.keys())}"
        )

    return ExchangeResult(
        access_token=access,
        refresh_token=None,    # Cursor doesn't issue one through this path
        expires_at=None,       # JWT carries its own exp; we don't decode it for v1
        id_token=None,
        raw=data,
    )


async def refresh_access_token(refresh_token: str) -> ExchangeResult:
    """Cursor v1 doesn't support refresh-token rotation via the
    cursor-bridge sidecar. Background tasks that try to refresh a
    cursor-oauth provider hit this and get a clear "not supported" error
    rather than a silent no-op or a misleading "invalid_grant".

    Operators re-onboard via the rotate endpoint when the JWT expires
    (~weeks at a time). v2 will replicate the IDE's
    ``POST api2.cursor.sh/oauth/token grant_type=refresh_token`` dance
    end-to-end so we can rotate automatically.
    """
    raise OAuthFlowError(
        "Cursor refresh-token rotation isn't supported in v4.4.31. "
        "Re-onboard the provider via the /cursor-oauth-rotate endpoint "
        "(the admin UI surfaces this as Re-authorize)."
    )


async def refresh_and_persist(provider, db) -> ExchangeResult:
    """Same surface as claude_oauth_flow / codex_oauth_flow.
    Currently a stub that raises — see ``refresh_access_token``."""
    return await refresh_access_token(getattr(provider, "oauth_refresh_token", "") or "")
