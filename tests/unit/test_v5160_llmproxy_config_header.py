"""v5.16.0 (#512) — Consolidated ``x-llmproxy-config`` request header.

Parsing edge cases + precedence + wiring pins.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


# ── (1) Parser semantics ──────────────────────────────────────────────


def test_parse_empty_returns_empty_dict():
    from app.api._llmproxy_config_header import parse_config_blob
    assert parse_config_blob(None) == {}
    assert parse_config_blob("") == {}


def test_parse_valid_object():
    from app.api._llmproxy_config_header import parse_config_blob
    r = parse_config_blob('{"accept_mcp":["fetch_url"]}')
    assert r == {"accept_mcp": ["fetch_url"]}


def test_parse_malformed_json_soft_fails():
    """Never raises — the header is a soft-fail path so a caller with
    a bad blob doesn't break their entire request."""
    from app.api._llmproxy_config_header import parse_config_blob
    assert parse_config_blob("not-json-at-all") == {}
    assert parse_config_blob('{"unclosed":') == {}
    assert parse_config_blob('{"accept_mcp":') == {}


def test_parse_non_object_top_level_is_ignored():
    from app.api._llmproxy_config_header import parse_config_blob
    assert parse_config_blob('["accept_mcp"]') == {}
    assert parse_config_blob('"just_a_string"') == {}
    assert parse_config_blob('42') == {}


def test_unknown_keys_silently_ignored():
    """Forward-compat: a caller can send a key we don't yet know about."""
    from app.api._llmproxy_config_header import parse_config_blob
    r = parse_config_blob('{"accept_mcp":["x"],"future_key":"value"}')
    assert r == {"accept_mcp": ["x"]}
    assert "future_key" not in r


# ── (2) Precedence: individual header wins ──────────────────────────


def _mock_request(headers: dict):
    r = MagicMock()
    r.headers = headers
    # Simulate FastAPI's request.state — a mutable namespace we set
    # attributes on. Use a plain object() and setattr / getattr.
    class _State: pass
    r.state = _State()
    return r


def test_individual_header_wins_over_blob():
    from app.api._llmproxy_config_header import read_config_key
    r = _mock_request({
        "x-llmproxy-config": '{"accept_mcp":["from-blob"]}',
        "X-Proxy-Accept-MCP": "from-individual-header",
    })
    v = read_config_key(r, "accept_mcp", header_fallback="X-Proxy-Accept-MCP")
    assert v == "from-individual-header"


def test_blob_used_when_no_individual_header():
    from app.api._llmproxy_config_header import read_config_key
    r = _mock_request({
        "x-llmproxy-config": '{"accept_mcp":["from-blob"]}',
    })
    v = read_config_key(r, "accept_mcp", header_fallback="X-Proxy-Accept-MCP")
    assert v == ["from-blob"]


def test_returns_none_when_neither_present():
    from app.api._llmproxy_config_header import read_config_key
    r = _mock_request({})
    v = read_config_key(r, "accept_mcp", header_fallback="X-Proxy-Accept-MCP")
    assert v is None


def test_empty_individual_header_falls_back_to_blob():
    """Empty string means 'unset'; blob should win in that case."""
    from app.api._llmproxy_config_header import read_config_key
    r = _mock_request({
        "x-llmproxy-config": '{"accept_mcp":["from-blob"]}',
        "X-Proxy-Accept-MCP": "",
    })
    v = read_config_key(r, "accept_mcp", header_fallback="X-Proxy-Accept-MCP")
    assert v == ["from-blob"]


# ── (3) Emit applied header ───────────────────────────────────────────


def test_emit_applied_header_no_op_when_no_blob():
    """No blob → no header added → keeps normal responses free of noise."""
    from app.api._llmproxy_config_header import emit_config_applied_header
    r = _mock_request({})
    headers = {}
    emit_config_applied_header(headers, r)
    assert "X-LLMProxy-Config-Applied" not in headers


def test_emit_applied_header_echoes_parsed_blob():
    from app.api._llmproxy_config_header import emit_config_applied_header
    r = _mock_request({
        "x-llmproxy-config": '{"accept_mcp":["fetch_url"]}',
    })
    headers = {}
    emit_config_applied_header(headers, r)
    assert "X-LLMProxy-Config-Applied" in headers
    import json as _j
    echoed = _j.loads(headers["X-LLMProxy-Config-Applied"])
    assert echoed == {"accept_mcp": ["fetch_url"]}


# ── (4) Wiring pins — messages.py + completions.py ──────────────────


def test_messages_reads_from_config_header():
    # v5.19.0 — messages.py's response-tail extracted to
    # _messages_response_tail.py. The config-header reads + emit both
    # live there now; check either file.
    files = [
        Path("app/api/messages.py"),
        Path("app/api/_messages_response_tail.py"),
    ]
    src = "\n".join(f.read_text() for f in files if f.exists())
    assert "from app.api._llmproxy_config_header import read_config_key" in src
    assert 'read_config_key(' in src
    # And emits the applied-header:
    assert 'emit_config_applied_header' in src


def test_completions_reads_from_config_header():
    src = Path("app/api/completions.py").read_text()
    assert "from app.api._llmproxy_config_header import read_config_key" in src
    assert 'read_config_key(' in src
    assert 'emit_config_applied_header' in src


# ── (5) Version bumped ────────────────────────────────────────────────


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 16, 0), (
        f"expected >= 5.16.0, got {major}.{minor}.{patch}"
    )
