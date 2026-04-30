"""
OpenAI Codex CLI OAuth provider (v3.0.15).

Consumes an OAuth access token captured by ``codex auth`` (the OpenAI Codex
CLI). Tokens are scoped to a ChatGPT subscription (Plus/Pro/Team/Enterprise)
and bill the user's subscription quota — not the API/billing-account budget.

Upstream surface (verified against a live Plus account on 2026-04-30):

  - Authentication: ``Authorization: Bearer <access_token>`` (JWT, ~2KB)
                    plus ``ChatGPT-Account-ID: <uuid>`` (from id_token JWT)
                    plus ``originator: codex_cli_rs``
                    plus ``User-Agent: codex_cli_rs/<ver> (...)``
  - Chat endpoint:  ``POST https://chatgpt.com/backend-api/codex/responses``
                    Body: OpenAI Responses API shape
                          (``input``, ``instructions``, ``stream``, ``store``,
                          ``model``, ``tools``, ``reasoning``, ``service_tier``)
                    Constraint: ``stream: true`` is REQUIRED — non-streaming
                    requests return 400 ``"Stream must be set to true"``.
  - Models:         ``GET .../models?client_version=<semver>`` returns the
                    list available to this account. Subscription tier
                    determines the slugs (e.g. Plus sees ``gpt-5.5``,
                    ``gpt-5.4``, ``gpt-5.4-mini``, ``gpt-5.3-codex``,
                    ``gpt-5.2``, ``codex-auto-review``; Pro/Team see more).
  - Token refresh:  see ``codex_oauth_flow.py::refresh_and_persist``.
                    Refresh rotates the refresh_token; persistence is
                    mandatory.

Why ``chatgpt.com/backend-api/codex/responses`` and not
``api.openai.com/v1/chat/completions``? The api.openai.com host uses
``sk-...`` API keys tied to a billing account. ChatGPT Plus subscriptions
don't get API keys. The chatgpt.com backend exposes a separate Codex
surface that accepts the OAuth bearer + workspace header and bills the
subscription.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional


CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_RESPONSES_URL = f"{CODEX_BASE_URL}/responses"
CODEX_MODELS_URL = f"{CODEX_BASE_URL}/models"

# These match a real Codex CLI install. The originator + User-Agent are
# load-bearing — the backend rejects unfamiliar values for some account
# tiers. ``codex_cli_rs`` is what the CLI itself sends.
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_USER_AGENT = "codex_cli_rs/0.128.0 (linux 6.1; x86_64) llm-proxy-v2"
CODEX_CLIENT_VERSION = "0.128.0"  # used for /models?client_version=...

# Token detection: Codex access_tokens are OpenAI JWTs (3 base64-url segments
# separated by '.'), so we sniff for the dot count when the user pastes a
# bare token rather than a JSON blob.
_JWT_TOKEN_PARTS = 3


@dataclass
class CodexOAuthCredentials:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None  # unix timestamp
    chatgpt_account_id: Optional[str] = None
    chatgpt_plan_type: Optional[str] = None
    id_token: Optional[str] = None


class CredentialParseError(ValueError):
    """Raised by parse_credentials when the blob can't be interpreted."""


def parse_credentials(raw: str) -> CodexOAuthCredentials:
    """Accept either a Codex CLI ``auth.json`` JSON blob or a bare access
    token (JWT). Mirrors claude_oauth.parse_credentials.

    Codex auth.json shape (from ``~/.codex/auth.json`` after ``codex auth``):
      {
        "OPENAI_API_KEY": null,                  // or sk-... for hybrid auth
        "tokens": {
          "id_token": "<JWT>",
          "access_token": "<JWT>",
          "refresh_token": "<opaque>",
          "account_id": "<chatgpt_account_id>"  // optional; usually parsed
                                                // from id_token if absent
        },
        "last_refresh": "2026-04-30T12:34:56Z"
      }
    """
    if not raw or not raw.strip():
        raise CredentialParseError("Credentials are empty")
    raw = raw.strip()

    # Bare JWT path — three base64-url segments joined by '.', no whitespace.
    if not raw.startswith("{") and raw.count(".") == _JWT_TOKEN_PARTS - 1:
        if any(c.isspace() for c in raw):
            raise CredentialParseError(
                "Bare token contains whitespace — did you paste a JSON value by mistake?"
            )
        return CodexOAuthCredentials(access_token=raw)

    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise CredentialParseError(f"Invalid JSON: {e}") from e

        # Most installs nest the tokens under "tokens" — flatten if so.
        tokens_blob = data.get("tokens")
        if isinstance(tokens_blob, dict):
            merged = {**data, **tokens_blob}
        else:
            merged = data

        access = (
            merged.get("access_token")
            or merged.get("accessToken")
        )
        if not access:
            raise CredentialParseError(
                "JSON is missing an 'access_token' field. Look for a key like "
                "'access_token' under the top-level or under a 'tokens' object "
                "in the file you pasted."
            )
        if not isinstance(access, str) or access.count(".") != _JWT_TOKEN_PARTS - 1:
            raise CredentialParseError(
                "access_token doesn't look like an OpenAI JWT (expected three "
                "dot-separated base64 segments)."
            )

        refresh = merged.get("refresh_token") or merged.get("refreshToken")
        id_token = merged.get("id_token") or merged.get("idToken")
        account_id = (
            merged.get("account_id")
            or merged.get("accountId")
            or merged.get("chatgpt_account_id")
        )
        plan_type = merged.get("chatgpt_plan_type") or merged.get("planType")
        expires_at = _extract_expiry(merged)

        # If account_id wasn't on the row but we have an id_token, decode it.
        if not account_id and id_token:
            from app.providers.codex_oauth_flow import parse_id_token
            account_id, plan_type_from_jwt = parse_id_token(id_token)
            if not plan_type:
                plan_type = plan_type_from_jwt

        return CodexOAuthCredentials(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=expires_at,
            chatgpt_account_id=account_id if isinstance(account_id, str) else None,
            chatgpt_plan_type=plan_type if isinstance(plan_type, str) else None,
            id_token=id_token if isinstance(id_token, str) else None,
        )

    raise CredentialParseError(
        "Input doesn't look like a Codex auth.json or a bare access-token JWT. "
        "Run `codex auth` then `cat ~/.codex/auth.json` and paste its output here."
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
        return float(v) / 1000.0 if float(v) > 32503680000 else float(v)
    if isinstance(v, str):
        from datetime import datetime
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


def build_headers(
    access_token: str, *, chatgpt_account_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict[str, str]:
    """Return the exact header set Codex CLI uses for chat calls.

    ``chatgpt_account_id`` is the workspace UUID; required when present
    (the backend uses it to scope the call to the right ChatGPT workspace).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": CODEX_ORIGINATOR,
        "User-Agent": CODEX_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if chatgpt_account_id:
        headers["ChatGPT-Account-ID"] = chatgpt_account_id
    if extra:
        headers.update(extra)
    return headers


def is_token_expired(expires_at: Optional[float], skew_seconds: float = 30.0) -> bool:
    """Lazy-refresh check; True if expired or within ``skew_seconds`` of expiring."""
    if expires_at is None:
        return False
    return time.time() >= (expires_at - skew_seconds)
