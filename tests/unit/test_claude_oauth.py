"""Unit tests for the claude-oauth credential parser + header builder."""
from __future__ import annotations

import sys
import time
import types
import pytest

# Stub heavy deps before app imports
_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.providers.claude_oauth import (
    parse_credentials,
    build_headers,
    is_token_expired,
    CredentialParseError,
    OAUTH_BETA_FLAGS,
    PLATFORM_BASE_URL,
    TOKEN_PREFIX,
)


SAMPLE_TOKEN = "sk-ant-oat01-abcDEF0123456789_-GhIjklMNopqrsTUVwxyz"


class TestParseCredentialsBareToken:
    def test_bare_token_parsed(self):
        creds = parse_credentials(SAMPLE_TOKEN)
        assert creds.access_token == SAMPLE_TOKEN
        assert creds.refresh_token is None
        assert creds.expires_at is None

    def test_bare_token_trimmed(self):
        creds = parse_credentials(f"   {SAMPLE_TOKEN}\n")
        assert creds.access_token == SAMPLE_TOKEN

    def test_bare_token_rejects_whitespace_middle(self):
        with pytest.raises(CredentialParseError):
            parse_credentials(SAMPLE_TOKEN[:20] + " " + SAMPLE_TOKEN[20:])


class TestParseCredentialsJson:
    def test_flat_access_token(self):
        creds = parse_credentials(f'{{"access_token":"{SAMPLE_TOKEN}"}}')
        assert creds.access_token == SAMPLE_TOKEN

    def test_camelcase_access_token(self):
        creds = parse_credentials(f'{{"accessToken":"{SAMPLE_TOKEN}"}}')
        assert creds.access_token == SAMPLE_TOKEN

    def test_refresh_token_extracted(self):
        raw = f'{{"access_token":"{SAMPLE_TOKEN}","refresh_token":"r3fr3sh"}}'
        creds = parse_credentials(raw)
        assert creds.refresh_token == "r3fr3sh"

    def test_refresh_token_empty_treated_as_none(self):
        raw = f'{{"access_token":"{SAMPLE_TOKEN}","refresh_token":""}}'
        creds = parse_credentials(raw)
        assert creds.refresh_token is None

    def test_expires_in_converted_to_absolute(self):
        raw = f'{{"access_token":"{SAMPLE_TOKEN}","expires_in":3600}}'
        creds = parse_credentials(raw)
        assert creds.expires_at is not None
        # Within a few seconds of now+3600
        assert abs(creds.expires_at - (time.time() + 3600)) < 5

    def test_expires_at_iso_parsed(self):
        raw = f'{{"access_token":"{SAMPLE_TOKEN}","expires_at":"2030-01-01T00:00:00Z"}}'
        creds = parse_credentials(raw)
        assert creds.expires_at is not None
        assert creds.expires_at > time.time()

    def test_expires_at_unix_seconds(self):
        future = int(time.time()) + 7200
        raw = f'{{"access_token":"{SAMPLE_TOKEN}","expires_at":{future}}}'
        creds = parse_credentials(raw)
        assert creds.expires_at == float(future)

    def test_expires_at_unix_milliseconds(self):
        future_ms = (int(time.time()) + 7200) * 1000
        raw = f'{{"access_token":"{SAMPLE_TOKEN}","expires_at":{future_ms}}}'
        creds = parse_credentials(raw)
        # Should detect ms and divide by 1000
        assert creds.expires_at is not None
        assert abs(creds.expires_at - (future_ms / 1000.0)) < 1

    def test_wrapped_claudeAiOauth_shape(self):
        """Claude Code stores credentials under a wrapper in some versions."""
        raw = (
            '{"claudeAiOauth":{'
            f'"accessToken":"{SAMPLE_TOKEN}",'
            '"refreshToken":"r3fr3sh",'
            '"expiresAt":"2030-01-01T00:00:00Z"}}'
        )
        creds = parse_credentials(raw)
        assert creds.access_token == SAMPLE_TOKEN
        assert creds.refresh_token == "r3fr3sh"
        assert creds.expires_at is not None

    def test_wrapped_credentials_shape(self):
        raw = f'{{"credentials":{{"access_token":"{SAMPLE_TOKEN}"}}}}'
        creds = parse_credentials(raw)
        assert creds.access_token == SAMPLE_TOKEN


class TestParseCredentialsErrors:
    def test_empty_rejected(self):
        with pytest.raises(CredentialParseError):
            parse_credentials("")
        with pytest.raises(CredentialParseError):
            parse_credentials("   \n  ")

    def test_invalid_json_rejected(self):
        with pytest.raises(CredentialParseError) as ei:
            parse_credentials('{"access_token": "sk-ant-oat01-..."')  # truncated
        assert "Invalid JSON" in str(ei.value)

    def test_wrong_prefix_rejected(self):
        # e.g. a standard API key pasted by mistake
        with pytest.raises(CredentialParseError) as ei:
            parse_credentials('{"access_token":"sk-ant-api03-abcdef"}')
        assert "doesn't look like a Claude OAuth token" in str(ei.value)

    def test_missing_access_token_rejected(self):
        with pytest.raises(CredentialParseError):
            parse_credentials('{"foo":"bar"}')

    def test_gibberish_rejected(self):
        with pytest.raises(CredentialParseError):
            parse_credentials("hello world")


class TestBuildHeaders:
    def test_authorization_header(self):
        h = build_headers(SAMPLE_TOKEN)
        assert h["Authorization"] == f"Bearer {SAMPLE_TOKEN}"

    def test_anthropic_version(self):
        h = build_headers(SAMPLE_TOKEN)
        assert h["anthropic-version"] == "2023-06-01"

    def test_oauth_beta_flag_present(self):
        h = build_headers(SAMPLE_TOKEN)
        # This is the marker beta flag that switches Anthropic to OAuth auth
        assert "oauth-2025-04-20" in h["anthropic-beta"]
        # And the claude-code one that switches routing to platform.claude.com
        assert "claude-code-20250219" in h["anthropic-beta"]

    def test_x_app_cli(self):
        h = build_headers(SAMPLE_TOKEN)
        assert h["x-app"] == "cli"

    def test_browser_access_flag(self):
        h = build_headers(SAMPLE_TOKEN)
        assert h["anthropic-dangerous-direct-browser-access"] == "true"

    def test_no_x_api_key(self):
        """If we accidentally send both `x-api-key` and `Authorization`,
        Anthropic 400s. This test guards against that."""
        h = build_headers(SAMPLE_TOKEN)
        assert "x-api-key" not in h
        assert "X-Api-Key" not in h

    def test_haiku_strips_long_context_beta(self):
        """Haiku doesn't grant context-1m-2025-08-07 at the Pro Max tier —
        keeping it in the header returns 400 'long context beta not yet available'."""
        h = build_headers(SAMPLE_TOKEN, model="claude-haiku-4-5-20251001")
        assert "context-1m-2025-08-07" not in h["anthropic-beta"]
        # Other flags still present
        assert "oauth-2025-04-20" in h["anthropic-beta"]
        assert "claude-code-20250219" in h["anthropic-beta"]

    def test_sonnet_4_6_keeps_long_context_beta(self):
        h = build_headers(SAMPLE_TOKEN, model="claude-sonnet-4-6")
        assert "context-1m-2025-08-07" in h["anthropic-beta"]

    def test_opus_4_7_keeps_long_context_beta(self):
        h = build_headers(SAMPLE_TOKEN, model="claude-opus-4-7")
        assert "context-1m-2025-08-07" in h["anthropic-beta"]

    def test_dated_sonnet_4_6_keeps_long_context_beta(self):
        # claude-sonnet-4-6-20251108 etc. should match the prefix
        h = build_headers(SAMPLE_TOKEN, model="claude-sonnet-4-6-20251108")
        assert "context-1m-2025-08-07" in h["anthropic-beta"]

    def test_older_sonnet_4_5_strips_long_context_beta(self):
        """v2.8.7: claude-sonnet-4-5-20250929 returns 400 with 1M flag.
        Whitelist excludes anything not sonnet-4-6/opus-4-7."""
        h = build_headers(SAMPLE_TOKEN, model="claude-sonnet-4-5-20250929")
        assert "context-1m-2025-08-07" not in h["anthropic-beta"]
        # Other flags still present
        assert "oauth-2025-04-20" in h["anthropic-beta"]

    def test_older_opus_strips_long_context_beta(self):
        h = build_headers(SAMPLE_TOKEN, model="claude-opus-4-1-20250805")
        assert "context-1m-2025-08-07" not in h["anthropic-beta"]

    def test_opus_4_5_strips_long_context_beta(self):
        # opus-4-5 / opus-4-6 not on the 1M whitelist either — only opus-4-7
        h = build_headers(SAMPLE_TOKEN, model="claude-opus-4-5-20251101")
        assert "context-1m-2025-08-07" not in h["anthropic-beta"]

    def test_no_model_keeps_full_flag_set(self):
        # When the model is unknown, default behaviour is to keep the full
        # set (caller can still override later with a model-aware call).
        # Actually v2.8.7 changed this: with no model context, we strip 1M.
        # Keeping the negative assertion documents the new behaviour.
        h = build_headers(SAMPLE_TOKEN)
        assert "context-1m-2025-08-07" not in h["anthropic-beta"]


def _stub_modules_for_streaming_import():
    """Stub heavy deps so importing app.api._messages_streaming works in unit tests."""
    import types as _t

    class _Noop:
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return self
        def labels(self, *a, **kw): return self
        def inc(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def set(self, *a, **kw): pass
        def info(self, *a, **kw): pass

    if "prometheus_client" not in sys.modules:
        m = _t.ModuleType("prometheus_client")
        m.CONTENT_TYPE_LATEST = "text/plain"
        m.Counter = _Noop
        m.Gauge = _Noop
        m.Histogram = _Noop
        m.Info = _Noop
        m.generate_latest = lambda: b""
        sys.modules["prometheus_client"] = m


class TestInjectClaudeCodeSystem:
    """v2.7.6 BUG-006 — injected marker carries cache_control so Anthropic's
    prompt cache keys remain stable across requests."""

    def setup_method(self):
        _stub_modules_for_streaming_import()

    def test_marker_has_cache_control(self):
        from app.api._messages_streaming import _inject_claude_code_system
        out = _inject_claude_code_system({"messages": []})
        marker = out["system"][0]
        assert marker["cache_control"] == {"type": "ephemeral"}

    def test_string_system_preserved_as_second_block(self):
        from app.api._messages_streaming import _inject_claude_code_system
        out = _inject_claude_code_system({"system": "user-system", "messages": []})
        assert len(out["system"]) == 2
        assert out["system"][0]["text"].startswith("You are Claude Code")
        assert out["system"][1]["text"] == "user-system"

    def test_already_marked_passes_through(self):
        from app.api._messages_streaming import _inject_claude_code_system
        body = {"system": [{"type": "text",
                              "text": "You are Claude Code, Anthropic's official CLI for Claude. extra"}],
                 "messages": []}
        out = _inject_claude_code_system(body)
        # No prepended marker block
        assert len(out["system"]) == 1


class TestIsTokenExpired:
    def test_none_returns_false(self):
        # Unknown expiry → trust the token until we see a 401
        assert is_token_expired(None) is False

    def test_far_future_returns_false(self):
        assert is_token_expired(time.time() + 3600) is False

    def test_past_returns_true(self):
        assert is_token_expired(time.time() - 60) is True

    def test_within_skew_returns_true(self):
        assert is_token_expired(time.time() + 10, skew_seconds=30) is True


class TestConstants:
    def test_platform_url_is_claude_com(self):
        # console.anthropic.com 302-redirects here for OAuth tokens —
        # we hit the final host directly to save a round-trip.
        assert PLATFORM_BASE_URL == "https://platform.claude.com"

    def test_token_prefix(self):
        assert TOKEN_PREFIX == "sk-ant-oat"

    def test_beta_flags_string(self):
        # Critical ones from the capture
        for flag in ("oauth-2025-04-20", "claude-code-20250219"):
            assert flag in OAUTH_BETA_FLAGS
