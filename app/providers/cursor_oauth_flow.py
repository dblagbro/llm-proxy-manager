"""v4.4.33 — Cursor (Pro/Business) onboarding via the real PKCE-style
deep-link flow Cursor's own IDE uses.

We mirror what the upstream ``Cursor-To-OpenAI`` sidecar does internally
(see /app/src/tool/cursorLogin.js in the digest-pinned image):

  1. Generate a PKCE pair:
       verifier = base64url(random_bytes(43))
       challenge = base64url(sha256(verifier))
     plus a fresh uuid4.
  2. Build the IDE login URL:
       https://cursor.com/loginDeepControl?challenge=<challenge>&uuid=<uuid>&mode=login&supportsSelectedTeamLogin=true
  3. Operator opens the URL, signs into Cursor (any team), and lands on
     /loginDeepPage. Cursor's backend internally pairs the (uuid,
     challenge) with the user's WorkOS session at that moment.
  4. We poll the IDE's auth-poll endpoint server-side:
       GET https://api2.cursor.sh/auth/poll?uuid=<uuid>&verifier=<verifier>
     A 200 with ``{accessToken, authId}`` means login completed; we
     synthesize the canonical ``user_<id>::<JWT>`` cookie from
     ``authId.split("|")[1] + "::" + accessToken`` (the IDE uses
     URL-encoded ``%3A%3A`` — same value, we store the bare form).

No DevTools cookie copy. No paste-the-token. Operator clicks Authorize,
logs in, the backend grabs the token via poll.

Compatibility:

- ``start_authorize()`` keeps the existing AuthorizeStart shape so the
  shared providers_oauth ``_do_authorize`` handler is unchanged.
- ``exchange_code()`` continues to accept a pasted cookie value (the
  v4.4.31 path) as a fallback, in case the poll endpoint changes or
  the operator's network can't reach api2.cursor.sh. The bridge sidecar
  ``/cursor/loginDeepControl`` does the cookie→token conversion as
  before.
- New ``poll_for_token(state)`` is the polished-path entrypoint the
  /cursor-oauth/poll API surfaces.
"""
from __future__ import annotations

import base64
import hashlib
import os as _os
import secrets
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import Optional

import httpx


# IDE-style login URL. ``supportsSelectedTeamLogin`` lets users choose a
# team after authenticating; harmless if they have only a personal account.
# We don't go through ``www.`` or ``/cn/`` — operator's live trace was on
# the apex ``cursor.com``.
CURSOR_LOGIN_DEEP_CONTROL = "https://cursor.com/loginDeepControl"

# Where the backend polls for the access token after the user logs in.
# This is api2.cursor.sh — Cursor's IDE backend, not the same host as the
# login URL. Reachable from the proxy container directly (no sidecar
# involvement in the polished path).
CURSOR_AUTH_POLL_URL = "https://api2.cursor.sh/auth/poll"

# Identify ourselves as the same Electron build the IDE poses as so the
# poll endpoint doesn't get cute with us. The exact version doesn't
# matter — Cursor's gateway accepts any reasonable IDE UA — but matching
# the sidecar's UA keeps us indistinguishable.
_CURSOR_POLL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Cursor/0.48.6 Chrome/132.0.6834.210 "
    "Electron/34.3.4 Safari/537.36"
)

# Backwards-compat alias for code/tests written against v4.4.31 — the
# old single landing-page URL. Still used as a last-resort fallback if
# the polished poll path is disabled. Not currently surfaced.
CURSOR_SIGNIN_URL = "https://www.cursor.com/dashboard"

# Sidecar bridge — only used by the fallback paste-the-cookie path.
CURSOR_BRIDGE_URL = _os.environ.get(
    "CURSOR_BRIDGE_URL",
    "http://llm-proxy2-cursor-bridge:3010",
)


# Pending-state TTL. Operator has 10 min from clicking Generate Auth
# URL until the entry sweeps; matches the codex_oauth_flow pattern.
_STATE_TTL_SEC = 600


@dataclass
class _PendingFlow:
    created_at: float
    # PKCE pair + uuid, present for polished poll-based flows. None
    # entries are legacy (paste-cookie-only) flows — left here as a
    # safety net but not used by the live UI from v4.4.33 onward.
    uuid: Optional[str] = None
    verifier: Optional[str] = None


_PENDING: dict[str, _PendingFlow] = {}


def _sweep_pending(now: Optional[float] = None) -> None:
    cutoff = (now if now is not None else time.time()) - _STATE_TTL_SEC
    for state, flow in list(_PENDING.items()):
        if flow.created_at < cutoff:
            _PENDING.pop(state, None)


def _b64url(raw: bytes) -> str:
    """RFC 7636 base64url without padding — matches Node's
    ``Buffer.toString('base64url')``."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` per upstream cursorLogin.js.

    43-byte random → base64url (no padding) → 58-char string. The
    challenge is sha256(verifier) base64url-encoded (43-char output).
    Cursor's gateway compares the SHA-256 of the verifier we send during
    poll against the challenge we put in the login URL.
    """
    verifier = _b64url(secrets.token_bytes(43))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass
class AuthorizeStart:
    state: str
    authorize_url: str


def start_authorize(scope: Optional[str] = None) -> AuthorizeStart:
    """Generate a fresh PKCE pair + uuid, store them under a new state,
    return the IDE-style login URL.

    ``scope`` is accepted-and-ignored for cross-vendor signature parity.
    """
    _sweep_pending()
    state = secrets.token_urlsafe(24)
    verifier, challenge = _generate_pkce_pair()
    flow_uuid = str(_uuid.uuid4())
    _PENDING[state] = _PendingFlow(
        created_at=time.time(),
        uuid=flow_uuid,
        verifier=verifier,
    )
    # Match the URL shape we observed in the live flow:
    #   https://cursor.com/loginDeepControl?challenge=…&uuid=…&mode=login&supportsSelectedTeamLogin=true
    url = (
        f"{CURSOR_LOGIN_DEEP_CONTROL}"
        f"?challenge={challenge}&uuid={flow_uuid}"
        f"&mode=login&supportsSelectedTeamLogin=true"
    )
    return AuthorizeStart(state=state, authorize_url=url)


@dataclass
class ExchangeResult:
    """Shape parity with codex/claude flows. Cursor doesn't issue a
    refresh token or an explicit expires_at via either the poll or
    the cookie path — the JWT carries its own exp claim with a
    multi-week lifetime."""
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[float]
    id_token: Optional[str]
    raw: dict


class OAuthFlowError(Exception):
    pass


# ── Polished path: backend polls Cursor's IDE endpoint ─────────────────


def _synthesize_user_token(access_token: str, auth_id: str) -> str:
    """Build the canonical ``user_<id>::<JWT>`` cookie from the poll
    response. Upstream sidecar uses ``%3A%3A`` (URL-encoded) but stores
    the bare ``::`` form here so the sidecar's per-request Authorization
    header passes it through cleanly.
    """
    parts = (auth_id or "").split("|")
    if len(parts) > 1 and parts[1]:
        return f"{parts[1]}::{access_token}"
    # Cursor's poll response sometimes returns the bare accessToken
    # without an authId pipeline (older endpoints). Pass it through
    # unmodified; the sidecar tolerates that shape too.
    return access_token


async def poll_for_token(
    state: str,
    *,
    max_attempts: int = 6,
    interval_sec: float = 5.0,
) -> ExchangeResult:
    """Poll Cursor's IDE auth endpoint until the operator's login
    completes (or we hit ``max_attempts``).

    Returns ExchangeResult on success. Raises OAuthFlowError on:
    - state expired / never started
    - api2.cursor.sh unreachable
    - poll timeout (operator closed the tab without completing login)
    - non-200 response shape we don't recognize

    The shared providers_oauth handler wraps this in the same Provider
    row create / Provider row update calls as exchange_code, so the
    on-success path is identical.
    """
    _sweep_pending()
    flow = _PENDING.get(state)
    if not flow:
        raise OAuthFlowError(
            "state not found (expired after 10 min, or never started "
            "via the Generate Auth URL button)"
        )
    if not flow.uuid or not flow.verifier:
        raise OAuthFlowError(
            "state was started without PKCE pair — paste the cookie "
            "via the legacy exchange path instead"
        )

    poll_url = (
        f"{CURSOR_AUTH_POLL_URL}"
        f"?uuid={flow.uuid}&verifier={flow.verifier}"
    )
    headers = {"User-Agent": _CURSOR_POLL_UA, "Accept": "*/*"}

    last_status: Optional[int] = None
    last_body_preview: str = ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for _attempt in range(max_attempts):
                try:
                    resp = await client.get(poll_url, headers=headers)
                except httpx.HTTPError as e:
                    last_status = None
                    last_body_preview = f"{type(e).__name__}: {e}"
                    # Network blip during polling — sleep and retry.
                    await _async_sleep(interval_sec)
                    continue

                last_status = resp.status_code
                last_body_preview = (resp.text or "")[:200]

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        # 200 but unparseable body — keep polling; could
                        # be an intermediate state with a transient body.
                        await _async_sleep(interval_sec)
                        continue
                    access = data.get("accessToken") if isinstance(data, dict) else None
                    if isinstance(access, str) and access:
                        auth_id = data.get("authId", "") if isinstance(data, dict) else ""
                        token = _synthesize_user_token(access, auth_id)
                        _PENDING.pop(state, None)
                        return ExchangeResult(
                            access_token=token,
                            refresh_token=None,
                            expires_at=None,
                            id_token=None,
                            raw=data,
                        )
                    # 200 without accessToken = "still waiting for login".
                    # Keep polling.
                await _async_sleep(interval_sec)
    finally:
        # If we time out, leave the state in _PENDING so the operator
        # can retry the poll from the modal without restarting the flow.
        # The TTL sweep will eventually clear it.
        pass

    raise OAuthFlowError(
        f"Cursor login didn't complete within "
        f"{int(max_attempts * interval_sec)}s "
        f"(last poll: status={last_status}, body={last_body_preview!r}). "
        "Close the Cursor tab, click Generate Auth URL again, and finish "
        "the login within 5 minutes."
    )


async def _async_sleep(sec: float) -> None:
    """Indirection so tests can monkeypatch the sleep without patching
    the stdlib for the whole interpreter."""
    import asyncio
    await asyncio.sleep(sec)


# ── Fallback path: operator pastes the cookie (v4.4.31 shape) ──────────


def extract_code_from_callback(raw: str) -> tuple[str, Optional[str]]:
    """Accept a pasted WorkosCursorSessionToken cookie value. The v4.4.33
    polished UI doesn't surface this path by default — the poll endpoint
    handles things end-to-end — but the shape is preserved so the
    existing providers_oauth exchange handler keeps working and so an
    operator with a cookie already in hand can still onboard."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Empty callback — paste the WorkosCursorSessionToken cookie value")

    for prefix in ("WorkosCursorSessionToken=", "Cookie: WorkosCursorSessionToken=", "Cookie:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
            break
    if ";" in raw:
        raw = raw.split(";", 1)[0].strip()

    if not raw:
        raise ValueError("Cookie value is empty after prefix strip")

    if "%3A%3A" not in raw and "::" not in raw:
        raise ValueError(
            "Doesn't look like a WorkosCursorSessionToken — expected "
            "a URL-encoded JWT with ``%3A%3A`` (or ``::``) separator. "
            "Make sure you copied the cookie value, not the cookie name."
        )

    return raw, None


async def exchange_code(
    state: str,
    code: str,
    *,
    expected_state: Optional[str] = None,
) -> ExchangeResult:
    """Fallback: pasted-cookie exchange via the sidecar's
    /cursor/loginDeepControl. v4.4.31 path, kept for operators who
    already have a cookie in hand or hit poll-path issues."""
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
        raise OAuthFlowError(
            f"Couldn't reach cursor-bridge sidecar at {CURSOR_BRIDGE_URL}: "
            f"{type(e).__name__}: {e}. Is llm-proxy2-cursor-bridge running?"
        ) from None

    if resp.status_code != 200:
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
        refresh_token=None,
        expires_at=None,
        id_token=None,
        raw=data,
    )


async def refresh_access_token(refresh_token: str) -> ExchangeResult:
    raise OAuthFlowError(
        "Cursor refresh-token rotation isn't supported in v4.4.33. "
        "Re-onboard the provider via the /cursor-oauth-rotate endpoint "
        "(the admin UI surfaces this as Re-authorize)."
    )


async def refresh_and_persist(provider, db) -> ExchangeResult:
    return await refresh_access_token(getattr(provider, "oauth_refresh_token", "") or "")
