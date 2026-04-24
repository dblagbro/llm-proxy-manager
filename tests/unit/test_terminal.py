"""Unit tests for app/api/oauth_capture/terminal.py (v2.6.0)."""
from __future__ import annotations

import sys
import types
import pytest

# Stub heavy deps before app imports, matching the pattern from other test files.
_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.api.oauth_capture.terminal import (
    _sidecar_http_url, _sidecar_ws_url, _build_env_block,
)
from app.api.oauth_capture.presets import PRESETS
from app.models.db import OAuthCaptureProfile


# ── _sidecar_ws_url ──────────────────────────────────────────────────────────


class TestSidecarWsUrl:
    def test_http_to_ws(self):
        assert _sidecar_ws_url("http://llm-proxy2-capture:4000") == "ws://llm-proxy2-capture:4000"

    def test_https_to_wss(self):
        assert _sidecar_ws_url("https://capture.example.com:4000") == "wss://capture.example.com:4000"

    def test_trailing_path_preserved(self):
        assert _sidecar_ws_url("http://sidecar:4000/base") == "ws://sidecar:4000/base"

    def test_unknown_scheme_falls_back_to_ws(self):
        # Defensive: a misconfigured https→non-ws-capable reverse proxy
        # shouldn't produce a broken "ftp://" target.
        assert _sidecar_ws_url("weird://host:4000").startswith("ws://")


# ── _sidecar_http_url ────────────────────────────────────────────────────────


class TestSidecarHttpUrl:
    def test_disabled_returns_none(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "capture_sidecar_enabled", False)
        assert _sidecar_http_url() is None

    def test_enabled_returns_url(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "capture_sidecar_enabled", True)
        monkeypatch.setattr(settings, "capture_sidecar_url", "http://sidecar:4000/")
        # Trailing slash stripped
        assert _sidecar_http_url() == "http://sidecar:4000"

    def test_enabled_but_no_url(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "capture_sidecar_enabled", True)
        monkeypatch.setattr(settings, "capture_sidecar_url", None)
        assert _sidecar_http_url() is None


# ── _build_env_block ─────────────────────────────────────────────────────────


def _mk_profile(preset_key: str = "claude-code", secret: str = "s3cret") -> OAuthCaptureProfile:
    p = OAuthCaptureProfile(
        name="test-profile",
        preset=preset_key,
        upstream_urls=["https://console.anthropic.com"],
        secret=secret,
        enabled=True,
    )
    return p


class TestBuildEnvBlock:
    def test_claude_code_env(self):
        # v2.6.2: URL is CLEAN — no ?cap=SECRET. Sidecar→proxy is trusted
        # by source (no X-Forwarded-For). Adding ?cap= to the env var broke
        # claude-code's URL composition when it appended /v1/messages.
        p = _mk_profile()
        env = _build_env_block(p, "http://llm-proxy2:3000")
        expected_url = "http://llm-proxy2:3000/api/oauth-capture/test-profile"
        assert env["ANTHROPIC_BASE_URL"] == expected_url
        assert env["ANTHROPIC_AUTH_URL"] == expected_url
        assert env["ANTHROPIC_API_URL"] == expected_url
        # Hint var always set
        assert env["LLM_PROXY_CAPTURE_PROFILE"] == "test-profile"

    def test_no_secret_query_ever(self):
        # The env var never carries ?cap= (see v2.6.2 fix). Both with and
        # without a secret on the profile, the URL is clean for the sidecar.
        p = _mk_profile()
        env = _build_env_block(p, "http://llm-proxy2:3000")
        assert "?cap=" not in env["ANTHROPIC_BASE_URL"]
        assert "?" not in env["ANTHROPIC_BASE_URL"]

    def test_strips_proxy_trailing_slash(self):
        p = _mk_profile()
        env = _build_env_block(p, "http://llm-proxy2:3000/")
        # No double-slash between base and /api
        assert "//api/" not in env["ANTHROPIC_BASE_URL"]

    def test_unknown_preset_returns_empty(self):
        p = _mk_profile(preset_key="nonsense")
        p.preset = "does-not-exist"
        assert _build_env_block(p, "http://x:1") == {}

    def test_custom_preset_has_no_env_vars_to_inject(self):
        # The "custom" preset declares no env_var_names, so only the
        # hint var ends up in the env block.
        p = _mk_profile(preset_key="custom")
        env = _build_env_block(p, "http://x:1")
        assert env == {"LLM_PROXY_CAPTURE_PROFILE": "test-profile"}


# ── Preset login_cmd shape ───────────────────────────────────────────────────


class TestPresetLoginCmds:
    def test_claude_code_has_login_cmd(self):
        assert PRESETS["claude-code"].login_cmd == "claude login"

    def test_presets_without_login_cmd_are_empty_string(self):
        # Only claude-code ships login_cmd in v2.6.0. The rest default to "".
        other_keys = [k for k in PRESETS if k != "claude-code"]
        for k in other_keys:
            assert PRESETS[k].login_cmd == "", (
                f"Preset {k!r} has a login_cmd but v2.6.0 should ship claude-code only. "
                "Update this test when adding a new vendor."
            )

    def test_all_login_cmds_start_with_whitelisted_binary(self):
        # Mirrors sidecar/capture-runner.py::CLI_WHITELIST. Kept small
        # intentionally; expand when a new vendor's sidecar support lands.
        WHITELIST = {"claude"}  # v2.6.0
        for key, preset in PRESETS.items():
            if not preset.login_cmd:
                continue
            first = preset.login_cmd.split()[0]
            assert first in WHITELIST, (
                f"Preset {key!r} login_cmd starts with {first!r} which is not in the "
                f"sidecar whitelist. Update CLI_WHITELIST first."
            )
