"""
Claude Code OAuth provider (v2.7.0).

Consumes an Anthropic OAuth access token (``sk-ant-oat01-...``) that was
obtained by running ``claude login`` on any machine that has Claude Code
installed. The admin pastes either the raw access token or the JSON
contents of their Claude Code credentials file into the "Add provider"
form; we extract the token, store it encrypted, and at request time
forward ``/v1/messages`` calls to ``platform.claude.com`` with the
OAuth-specific header set that the CLI itself uses.

Why ``platform.claude.com`` and not ``api.anthropic.com``?
    Captured by v2.5.0's OAuth-capture passthrough: calls from Claude
    Code with an ``sk-ant-oat01-`` token hit ``console.anthropic.com/
    v1/messages`` which 302-redirects to ``platform.claude.com/v1/
    messages``. We skip the redirect and hit the final host directly.

Why the beta flag set?
    Claude Code sends a specific bundle of beta flags on every
    OAuth-authenticated request (``oauth-2025-04-20``, ``claude-code-
    20250219``, etc.). Missing flags cause 4xx responses. The exact set
    below matches the observed capture from 2026-04-24.

Token refresh is not implemented in v2.7.0 — when the token expires
the request will 401 and the admin must paste a new one. v2.7.x will
add the refresh flow once we capture a fresh ``claude login`` that
exposes the refresh endpoint.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


PLATFORM_BASE_URL = "https://platform.claude.com"

# Exact beta-flag set Claude Code sends with OAuth-authenticated requests.
# Observed in captured traffic on 2026-04-24. Order-insensitive on the
# server side, but we keep it in Claude-Code's order for parity so any
# header logging matches.
OAUTH_BETA_FLAGS = (
    "claude-code-20250219,"
    "oauth-2025-04-20,"
    "context-1m-2025-08-07,"
    "interleaved-thinking-2025-05-14,"
    "redact-thinking-2026-02-12,"
    "context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,"
    "advisor-tool-2026-03-01,"
    "effort-2025-11-24"
)

# v2.8.7: which models get the 1M-context beta on the Claude Pro Max
# subscription. Sending ``context-1m-2025-08-07`` to a model the
# subscription doesn't grant 1M for produces a 400:
#   ``"The long context beta is not yet available for this subscription."``
# This errored for haiku originally (v2.7.5 fix) and now for older Sonnet
# snapshots like ``claude-sonnet-4-5-20250929``. Switch to a whitelist —
# only the latest-tier Sonnet 4-6 and Opus 4-7 (and their dated variants)
# get the 1M flag; everything else falls back to the model's native
# context window (~200K). Short prompts continue to work; over-long ones
# get a normal context_length_exceeded error from upstream instead of
# the misleading "subscription" 400.
_LONG_CONTEXT_MODEL_PATTERNS = (
    "sonnet-4-6",   # claude-sonnet-4-6, claude-sonnet-4-6-YYYYMMDD
    "opus-4-7",     # claude-opus-4-7, claude-opus-4-7-YYYYMMDD
)


def _beta_flags_for_model(model: str) -> str:
    """Return the anthropic-beta flag bundle suitable for ``model``.

    Strips ``context-1m-2025-08-07`` for every model that isn't on the
    short whitelist of Pro-Max-1M-eligible families. Keeps every other
    flag (oauth, claude-code, interleaved-thinking, prompt-caching, …).
    """
    model_lc = (model or "").lower()
    grants_1m = any(p in model_lc for p in _LONG_CONTEXT_MODEL_PATTERNS)
    if grants_1m:
        return OAUTH_BETA_FLAGS
    parts = [f.strip() for f in OAUTH_BETA_FLAGS.split(",")]
    return ",".join(p for p in parts if p != "context-1m-2025-08-07")

ANTHROPIC_API_VERSION = "2023-06-01"

TOKEN_PREFIX = "sk-ant-oat"  # covers sk-ant-oat01-... and any future variant


@dataclass
class ClaudeOAuthCredentials:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None  # unix timestamp


class CredentialParseError(ValueError):
    """Raised by parse_credentials when the blob can't be interpreted."""


def parse_credentials(raw: str) -> ClaudeOAuthCredentials:
    """Accept either a bare ``sk-ant-oat...`` access token or the JSON
    contents of a Claude Code credentials file.

    The JSON shape we look for (any of these key names works, since the
    CLI has used different layouts across versions):

        access_token | accessToken
        refresh_token | refreshToken
        expires_at | expiresAt  (ISO string or unix int/float)
        expires_in | expiresIn  (seconds from now)

    Raises ``CredentialParseError`` on any problem the admin can fix.
    """
    if not raw or not raw.strip():
        raise CredentialParseError("Credentials are empty")
    raw = raw.strip()

    # Bare token path
    if raw.startswith(TOKEN_PREFIX):
        # Sanity — no stray whitespace or JSON artifacts
        if any(c.isspace() for c in raw):
            raise CredentialParseError(
                "Bare token contains whitespace — did you paste a JSON value by mistake?"
            )
        return ClaudeOAuthCredentials(access_token=raw)

    # JSON path
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise CredentialParseError(f"Invalid JSON: {e}") from e

        access = data.get("access_token") or data.get("accessToken")
        if not access:
            # Some Claude Code versions wrap everything in a top-level key
            wrapper = data.get("credentials") or data.get("claudeAiOauth") or {}
            if isinstance(wrapper, dict):
                access = wrapper.get("access_token") or wrapper.get("accessToken")
                # Rebind data so downstream lookups see the wrapped shape
                data = {**data, **wrapper}
        if not access:
            raise CredentialParseError(
                "JSON is missing an 'access_token' field. Look for a key like "
                "'access_token' or 'accessToken' in the file you pasted."
            )
        if not isinstance(access, str) or not access.startswith(TOKEN_PREFIX):
            raise CredentialParseError(
                f"access_token doesn't look like a Claude OAuth token "
                f"(expected prefix {TOKEN_PREFIX!r}, got {access[:20]!r}...)"
            )

        refresh = data.get("refresh_token") or data.get("refreshToken")
        expires_at = _extract_expiry(data)

        return ClaudeOAuthCredentials(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=expires_at,
        )

    raise CredentialParseError(
        "Input doesn't look like a Claude Code credentials JSON or a bare "
        f"'{TOKEN_PREFIX}...' token. Run `cat ~/.claude/credentials.json` "
        "after `claude login` and paste its output here."
    )


def _extract_expiry(data: dict) -> Optional[float]:
    if "expires_at" in data or "expiresAt" in data:
        return _to_unix_ts(data.get("expires_at") or data.get("expiresAt"))
    if "expires_in" in data or "expiresIn" in data:
        seconds = data.get("expires_in") or data.get("expiresIn")
        try:
            return time.time() + float(seconds)
        except (TypeError, ValueError):
            return None
    return None


def _to_unix_ts(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        # Heuristic: Claude Code sometimes stores expires_at as milliseconds
        # since epoch. Anything beyond the year 3000 in seconds is almost
        # certainly actually ms.
        return float(v) / 1000.0 if float(v) > 32503680000 else float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            pass
    return None


# ── Request-side helpers ─────────────────────────────────────────────────────


def build_headers(access_token: str, model: Optional[str] = None) -> dict[str, str]:
    """Return the exact header set Claude Code uses for OAuth-authenticated
    ``/v1/messages`` requests. Mirroring the CLI's headers avoids subtle
    400s from the beta-flag server-side enforcement.

    ``model`` is optional; when provided, we prune beta flags the model's
    tier doesn't grant (e.g. strip ``context-1m-2025-08-07`` for Haiku).
    """
    return {
        "Authorization": f"Bearer {access_token}",
        "anthropic-version": ANTHROPIC_API_VERSION,
        "anthropic-beta": _beta_flags_for_model(model or ""),
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
    }


def is_token_expired(expires_at: Optional[float], skew_seconds: float = 30.0) -> bool:
    """Lazy-refresh check. Returns True if the access token is expired OR
    within ``skew_seconds`` of expiring."""
    if expires_at is None:
        return False  # don't know — assume valid, let the upstream 401 drive refresh
    return time.time() >= (expires_at - skew_seconds)
