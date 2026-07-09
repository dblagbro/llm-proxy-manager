"""v5.18.0 — outbound substitution-callback POST hook.

Closes the gap I owed hub team since 2026-06-30 (see 2026-07-02 reply memo).
v5.14 shipped the inbound registry only; v5.18.0 adds the outbound emitter
that POSTs a substitution event to hub's
``/api/compliance/callbacks/substitution`` receiver.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── (1) Module surface ────────────────────────────────────────────────


def test_hook_module_importable():
    from app.compliance.substitution_callback_hook import (
        compliance_substitution_callback_hook,
        _api_key_alias,
        _build_event,
        _post_once,
    )


def test_hook_registered_in_builtin_registration():
    src = Path("app/api/_response_hook_runner.py").read_text()
    assert "from app.compliance.substitution_callback_hook import" in src
    assert '"compliance_substitution_callback_hook"' in src


# ── (2) No-op semantics ──────────────────────────────────────────────


def test_noop_when_not_substituted():
    """Hook MUST NOT POST when substitution didn't happen."""
    from app.compliance.substitution_callback_hook import (
        compliance_substitution_callback_hook,
    )
    from app.api._response_hook_runner import HookContext

    ctx = HookContext(substituted=False)
    with patch("httpx.AsyncClient") as mock_client_cls:
        result = asyncio.run(compliance_substitution_callback_hook(
            handler_id="messages", resp_headers={}, context=ctx,
        ))
    assert result is None
    mock_client_cls.assert_not_called()


def test_noop_when_url_empty():
    """Empty URL setting = safe default = hook is a no-op."""
    from app.compliance.substitution_callback_hook import (
        compliance_substitution_callback_hook,
    )
    from app.api._response_hook_runner import HookContext
    from app.config import settings as _s

    orig_url = _s.substitution_callback_url
    _s.substitution_callback_url = ""
    try:
        ctx = HookContext(substituted=True, requested_model="a", served_model="b")
        with patch("httpx.AsyncClient") as mock_client_cls:
            result = asyncio.run(compliance_substitution_callback_hook(
                handler_id="messages", resp_headers={}, context=ctx,
            ))
        assert result is None
        mock_client_cls.assert_not_called()
    finally:
        _s.substitution_callback_url = orig_url


# ── (3) Event body shape (LiteLLM keys — hub option #3) ─────────────


def test_event_body_uses_litellm_shape():
    from app.compliance.substitution_callback_hook import _build_event
    from app.api._response_hook_runner import HookContext

    mock_key = MagicMock()
    mock_key.label = "coordinator-hub"

    ctx = HookContext(
        substituted=True,
        requested_model="claude-opus-4-6",
        served_model="claude-sonnet-4-6",
        compliance_event_id="audit_abc123",
        key_record=mock_key,
        extra={"substitution_reason": "cross_family_substitution"},
    )
    body = _build_event(ctx)
    assert body["original_model"] == "claude-opus-4-6"
    assert body["model"] == "claude-sonnet-4-6"
    assert body["substitution"] is True
    assert body["id"] == "audit_abc123"
    assert body["user_api_key_alias"] == "coordinator-hub"
    assert body["reason"] == "cross_family_substitution"
    assert isinstance(body["timestamp"], float)
    assert body["timestamp"] > 1_700_000_000  # sane epoch


def test_event_body_prefers_label_over_name():
    """When both label + name are set, label wins."""
    from app.compliance.substitution_callback_hook import _api_key_alias
    key = MagicMock()
    key.label = "custom-alias"
    key.name = "internal-name"
    assert _api_key_alias(key) == "custom-alias"

    key2 = MagicMock()
    key2.label = None
    key2.name = "fallback-name"
    assert _api_key_alias(key2) == "fallback-name"


# ── (4) POST behavior + retry ───────────────────────────────────────


def test_post_once_returns_ok_on_2xx():
    from app.compliance.substitution_callback_hook import _post_once
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    ok, code, err = asyncio.run(_post_once(
        mock_client, "http://hub/callback", {"x": 1}, {},
    ))
    assert ok is True
    assert code == 202
    assert err is None


def test_post_once_returns_failure_on_5xx():
    from app.compliance.substitution_callback_hook import _post_once
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    ok, code, err = asyncio.run(_post_once(
        mock_client, "http://hub/callback", {}, {},
    ))
    assert ok is False
    assert code == 503
    assert err == "http_503"


def test_post_once_returns_failure_on_transport_error():
    import httpx
    from app.compliance.substitution_callback_hook import _post_once
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

    ok, code, err = asyncio.run(_post_once(
        mock_client, "http://hub/callback", {}, {},
    ))
    assert ok is False
    assert code is None
    assert err == "ConnectError"


# ── (5) Auth header ─────────────────────────────────────────────────


def test_auth_header_uses_x_proxy_callback_token():
    """The header name MUST match hub's v2.6.6 receiver default."""
    src = Path("app/compliance/substitution_callback_hook.py").read_text()
    assert '"X-Proxy-Callback-Token"' in src


def test_no_auth_header_when_secret_empty():
    """Empty secret = no auth header sent (dev-mode passthrough)."""
    src = Path("app/compliance/substitution_callback_hook.py").read_text()
    # The header is only added if `secret.strip()` is truthy.
    assert "if secret.strip():" in src


# ── (6) Wiring pins ─────────────────────────────────────────────────


def test_messages_handler_passes_substitution_reason_in_extra():
    # v5.19.0 — response-tail extracted; the HookContext build now
    # lives in _messages_response_tail.py. Same intent: substitution_reason
    # + compliance_event_id + compliance_audit_id must all be threaded
    # through to the hook context on the messages path.
    files = [
        Path("app/api/messages.py"),
        Path("app/api/_messages_response_tail.py"),
    ]
    src = "\n".join(f.read_text() for f in files if f.exists())
    assert "substitution_reason" in src
    assert "compliance_substitution_reason" in src
    assert 'compliance_event_id=getattr(route, "compliance_audit_id"' in src


def test_settings_present_with_safe_defaults():
    from app.config import settings
    assert hasattr(settings, "substitution_callback_url")
    assert hasattr(settings, "substitution_callback_shared_secret")
    assert settings.substitution_callback_url == ""
    assert settings.substitution_callback_shared_secret == ""


# ── (7) Version ─────────────────────────────────────────────────────


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 18, 0), (
        f"expected >= 5.18.0, got {major}.{minor}.{patch}"
    )
