"""SSO integration — Wave 6.

Provides an OIDC-based Single Sign-On flow. The implementation is
deliberately dependency-light: it uses httpx (already required) for the
OIDC token exchange rather than pulling in authlib or python3-saml. An
operator who needs true SAML2 should install python3-saml and plug the
assertion-parsing shim at _parse_saml_assertion().

Flow (OIDC authorization-code):
    GET  /api/auth/sso/start    → redirects to IdP authorize endpoint
    GET  /api/auth/sso/callback → exchanges code for ID token, creates session

Configuration (settings / env):
    SSO_ENABLED=true
    SSO_ISSUER=https://accounts.google.com
    SSO_CLIENT_ID=...
    SSO_CLIENT_SECRET=...
    SSO_REDIRECT_URI=https://your-proxy/api/auth/sso/callback
    SSO_DEFAULT_ROLE=viewer

The sso_* fields on settings that mention SAML (SSO_ENTITY_ID,
SSO_IDP_METADATA_URL, SSO_ACS_URL) are wired but parsing a SAMLResponse
requires python3-saml to be installed; otherwise a 501 is returned.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import logging
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


# ── PKCE helpers ─────────────────────────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge). S256 method."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def generate_nonce() -> str:
    return secrets.token_urlsafe(24)


# ── OIDC claims parsing ──────────────────────────────────────────────────────


@dataclass
class SSOIdentity:
    subject: str           # "sub" claim — stable user ID at the IdP
    email: Optional[str]
    name: Optional[str]
    groups: list[str]      # often in "groups" or "roles" claim
    raw_claims: dict


def parse_id_token_claims(id_token: str) -> dict:
    """Decode the payload of a JWT id_token WITHOUT signature verification.
    Callers MUST NOT call this on untrusted tokens — use a proper verifier
    (authlib.jose or python-jose) in production. This helper exists for
    unit-testable payload introspection only.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT: must have 3 dot-separated segments")
    payload_b64 = parts[1]
    # Add padding
    pad = 4 - (len(payload_b64) % 4)
    if pad < 4:
        payload_b64 += "=" * pad
    import json
    return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))


def extract_identity(claims: dict) -> SSOIdentity:
    """Map OIDC claims to our SSOIdentity dataclass."""
    groups = claims.get("groups") or claims.get("roles") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    return SSOIdentity(
        subject=claims.get("sub") or "",
        email=claims.get("email"),
        name=claims.get("name") or claims.get("preferred_username"),
        groups=list(groups),
        raw_claims=claims,
    )


# ── Group → role mapping ─────────────────────────────────────────────────────


_DEFAULT_GROUP_MAP = {
    "llm-proxy-admin":    "admin",
    "llm-proxy-operator": "operator",
    "llm-proxy-viewer":   "viewer",
}


def role_from_groups(groups: list[str], default: str = "viewer") -> str:
    """Pick the most-privileged role from the user's IdP groups."""
    priority = {"admin": 3, "operator": 2, "viewer": 1}
    best = default
    for g in groups:
        mapped = _DEFAULT_GROUP_MAP.get(g)
        if mapped and priority.get(mapped, 0) > priority.get(best, 0):
            best = mapped
    return best


# ── SAML placeholder ─────────────────────────────────────────────────────────


def _parse_saml_assertion(saml_response_b64: str) -> SSOIdentity:
    """Parse a base64-encoded SAMLResponse. Requires python3-saml.

    This is a placeholder; real deployments should install python3-saml
    (and its native dependency xmlsec) and replace this function with one
    that validates the IdP signature chain. Returning a 501 keeps the
    surface honest about what's shipped.
    """
    raise NotImplementedError(
        "SAML2 binding requires python3-saml. Install it and plug into "
        "app/auth/sso.py:_parse_saml_assertion()."
    )
