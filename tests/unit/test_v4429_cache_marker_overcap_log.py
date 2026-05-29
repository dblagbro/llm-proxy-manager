"""v4.4.29 — observability for callers sending >4 cache_control markers.

BUG-085: coordinator-hub started producing 400 errors from Anthropic
on 2026-05-29: "A maximum of 4 blocks with cache_control may be
provided. Found 5." The proxy's `_inject_claude_code_system` already
caps its own injection (only adds `cache_control` to its marker block
when the caller's existing count is <4), so the 5 is caller-supplied.

Pre-v4.4.29 the only place "Found 5" showed up was the upstream error
text in `event_meta.error`. To diagnose hub-vs-proxy we had to
body-sample (1% rate) and parse the request. v4.4.29 adds an explicit
proxy-side warning log when the caller's count > 4, with a breakdown
across `sys` / `msgs` / `tools` so the source location is obvious.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_overcap_warning_source_guard():
    """The warning must exist + carry the breakdown by sub-location."""
    src = Path("app/api/_messages_streaming_oauth.py").read_text()
    assert "BUG-085" in src, "expected the v4.4.29 BUG-085 reference"
    # The four fields the warning must surface
    for field in ("sys=%d", "msgs=%d", "tools=%d"):
        assert field in src, f"warning must include {field} breakdown"
    # Must guarantee the proxy isn't itself adding the 5th marker
    assert "Proxy did NOT add its own marker's cache_control" in src


def test_overcap_does_not_break_dispatch_path():
    """The breakdown code is wrapped in try/except so a telemetry bug
    can never block real dispatch."""
    src = Path("app/api/_messages_streaming_oauth.py").read_text()
    idx = src.index("BUG-085")
    block = src[idx:idx + 2500]
    assert "try:" in block, "breakdown must be try-wrapped"
    assert "except Exception" in block, "must swallow telemetry failures"


def test_overcap_warning_fires_on_5_markers(caplog):
    """Behavioral: drive `_inject_claude_code_system` with a body that
    carries 5 cache_control markers (the exact BUG-085 shape) and assert
    the warning fires with the right counts. Don't fail if the cap
    breakdown loses the exact totals — verify it surfaces *something*
    that points an operator at the source."""
    import logging
    from app.api._messages_streaming_oauth import _inject_claude_code_system

    # 5 markers: 2 in system, 2 in messages, 1 in tools
    body = {
        "system": [
            {"type": "text", "text": "sysA", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "sysB", "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "u1", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "u2", "cache_control": {"type": "ephemeral"}},
            ],
        }],
        "tools": [{"name": "t", "cache_control": {"type": "ephemeral"}}],
    }

    with caplog.at_level(logging.WARNING, logger="app.api._messages_streaming_oauth"):
        _inject_claude_code_system(body)

    matches = [r for r in caplog.records if "cache_control markers" in r.message]
    assert matches, (
        "warning must fire when caller sends >4 cache_control markers; "
        f"records: {[r.message for r in caplog.records]}"
    )
    msg = matches[0].getMessage()
    # The actual count
    assert "5 cache_control" in msg, f"warning should report 5 markers: {msg}"
    # Breakdown
    assert "sys=2" in msg, f"breakdown should report sys=2: {msg}"
    assert "msgs=2" in msg, f"breakdown should report msgs=2: {msg}"
    assert "tools=1" in msg, f"breakdown should report tools=1: {msg}"


def test_no_warning_at_or_below_4_markers(caplog):
    """The warning fires only when count > 4 — at exactly 4 (the cap)
    the proxy correctly omits its own cache_control and the request
    will succeed; no warning needed and no log noise."""
    import logging
    from app.api._messages_streaming_oauth import _inject_claude_code_system

    body_4 = {
        "system": [
            {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "y", "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "u", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "v", "cache_control": {"type": "ephemeral"}},
            ],
        }],
    }
    with caplog.at_level(logging.WARNING, logger="app.api._messages_streaming_oauth"):
        _inject_claude_code_system(body_4)
    assert not any("cache_control markers" in r.message for r in caplog.records), (
        "no overcap warning expected at exactly 4 markers"
    )


def test_conftest_admin_pass_not_in_plaintext():
    """The plaintext admin password was committed in tests/conftest.py
    for the lifetime of the public repo. v4.4.29 moves it to an env
    var with a documented dev-default fallback so no real credential
    sits in source any longer."""
    src = Path("tests/conftest.py").read_text()
    assert "REMOVED-CREDENTIAL-ROTATED-20260828" not in src, (
        "plaintext production admin password must not be in source"
    )
    # Env var must be the source of truth
    assert "LLMPROXY_TEST_ADMIN_PASS" in src, (
        "ADMIN_PASS must read from LLMPROXY_TEST_ADMIN_PASS env var"
    )
