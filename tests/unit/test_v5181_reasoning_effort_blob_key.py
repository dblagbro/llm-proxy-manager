"""v5.18.1 (#512 additive) — reasoning_effort migrated to x-llmproxy-config blob.

Second key migrated after ``accept_mcp`` in v5.16.0. Callers can now pass
``reasoning_effort`` in the config blob as an alternative to the request
body param.
"""
from __future__ import annotations
from pathlib import Path


def test_reasoning_effort_added_to_known_keys():
    from app.api._llmproxy_config_header import KNOWN_KEYS
    assert "reasoning_effort" in KNOWN_KEYS


def test_reasoning_effort_wired_at_completions():
    src = Path("app/api/completions.py").read_text()
    assert "from app.api._llmproxy_config_header import read_config_key" in src
    assert 'read_config_key(request, "reasoning_effort")' in src


def test_body_still_wins_over_blob():
    """Existing per-request semantics preserved: body value takes
    precedence over blob value. Only fall through to blob when body
    is absent/empty."""
    src = Path("app/api/completions.py").read_text()
    assert "_re_from_body = body.get(" in src
    assert "if not _re_from_body:" in src


def test_reasoning_effort_parse_from_blob():
    """Integration: parse a blob containing reasoning_effort → get it back."""
    from app.api._llmproxy_config_header import parse_config_blob
    r = parse_config_blob('{"reasoning_effort":"high"}')
    assert r == {"reasoning_effort": "high"}


def test_reasoning_effort_and_accept_mcp_both_survive():
    """Callers combining reasoning_effort + accept_mcp in one blob get
    both keys back."""
    from app.api._llmproxy_config_header import parse_config_blob
    r = parse_config_blob('{"reasoning_effort":"high","accept_mcp":["fetch_url"]}')
    assert r == {"reasoning_effort": "high", "accept_mcp": ["fetch_url"]}


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 18, 1), (
        f"expected >= 5.18.1, got {major}.{minor}.{patch}"
    )
