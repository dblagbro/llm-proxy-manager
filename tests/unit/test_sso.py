"""Unit tests for SSO (OIDC) helpers (Wave 6)."""
import sys
import types
import base64
import json
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.auth.sso import (
    generate_pkce_pair, generate_state, generate_nonce,
    parse_id_token_claims, extract_identity, role_from_groups,
    _parse_saml_assertion,
)


def _make_jwt(payload: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    # Signature is irrelevant for payload parsing
    return f"{header_b64}.{payload_b64}.fake-sig"


class TestPKCE:
    def test_returns_two_different_strings(self):
        v, c = generate_pkce_pair()
        assert v != c
        assert len(v) > 30
        assert len(c) > 30

    def test_challenge_is_sha256_of_verifier(self):
        import hashlib
        v, c = generate_pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        assert c == expected

    def test_each_call_different(self):
        a = generate_pkce_pair()
        b = generate_pkce_pair()
        assert a != b


class TestStateNonce:
    def test_state_is_url_safe(self):
        s = generate_state()
        assert "/" not in s and "+" not in s

    def test_nonce_uniqueness(self):
        assert generate_nonce() != generate_nonce()


class TestParseIdTokenClaims:
    def test_parses_valid_jwt(self):
        token = _make_jwt({"sub": "user-42", "email": "alice@example.com"})
        claims = parse_id_token_claims(token)
        assert claims["sub"] == "user-42"
        assert claims["email"] == "alice@example.com"

    def test_rejects_malformed(self):
        with pytest.raises(ValueError):
            parse_id_token_claims("not.a.jwt.really")
        with pytest.raises(ValueError):
            parse_id_token_claims("onesegmentonly")


class TestExtractIdentity:
    def test_minimal_claims(self):
        identity = extract_identity({"sub": "abc-123"})
        assert identity.subject == "abc-123"
        assert identity.email is None
        assert identity.name is None
        assert identity.groups == []

    def test_full_claims(self):
        claims = {
            "sub": "abc-123",
            "email": "alice@corp.com",
            "name": "Alice",
            "groups": ["llm-proxy-admin", "other-group"],
        }
        identity = extract_identity(claims)
        assert identity.email == "alice@corp.com"
        assert identity.name == "Alice"
        assert "llm-proxy-admin" in identity.groups

    def test_roles_claim_also_parsed(self):
        identity = extract_identity({"sub": "x", "roles": ["llm-proxy-viewer"]})
        assert identity.groups == ["llm-proxy-viewer"]

    def test_groups_as_string(self):
        """Some IdPs send groups as a comma-separated string."""
        identity = extract_identity({"sub": "x", "groups": "a, b, c"})
        assert identity.groups == ["a", "b", "c"]

    def test_preferred_username_fallback(self):
        identity = extract_identity({"sub": "x", "preferred_username": "alice123"})
        assert identity.name == "alice123"


class TestRoleFromGroups:
    def test_admin_group_maps(self):
        assert role_from_groups(["llm-proxy-admin"]) == "admin"

    def test_operator_group_maps(self):
        assert role_from_groups(["llm-proxy-operator"]) == "operator"

    def test_viewer_group_maps(self):
        assert role_from_groups(["llm-proxy-viewer"]) == "viewer"

    def test_unknown_group_falls_back_to_default(self):
        assert role_from_groups(["random-group"]) == "viewer"
        assert role_from_groups(["random-group"], default="operator") == "operator"

    def test_empty_groups_returns_default(self):
        assert role_from_groups([]) == "viewer"

    def test_most_privileged_wins(self):
        """When user has multiple mapped groups, pick the highest privilege."""
        assert role_from_groups(["llm-proxy-viewer", "llm-proxy-admin"]) == "admin"
        assert role_from_groups(["llm-proxy-operator", "llm-proxy-viewer"]) == "operator"


class TestSAMLPlaceholder:
    def test_raises_not_implemented(self):
        """SAML is a placeholder until python3-saml is installed."""
        with pytest.raises(NotImplementedError):
            _parse_saml_assertion("some-base64-saml")
