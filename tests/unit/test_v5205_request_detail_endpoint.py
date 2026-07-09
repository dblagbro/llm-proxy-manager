"""v5.20.5 — /api/admin/requests/detail/{id} companion endpoint.

Companion to the v5.11.0 SSE stream. Operator sees a row in the
live-tail, wants full context: this endpoint returns the row +
provider summary + api_key summary (redacted) + correlated
activity_log events within ±30s of the same api_key+provider.

Ported from ccflare's /api/requests/detail (2026-06-30 peer-comparison
roadmap).
"""
from __future__ import annotations
from pathlib import Path


def test_endpoint_registered():
    src = Path("app/api/admin_requests_stream.py").read_text()
    assert '@router.get("/requests/detail/{activity_log_id}")' in src
    assert "async def request_detail" in src


def test_endpoint_returns_404_for_unknown_id():
    """Absent rows must 404, not silently return an empty row. Protects
    against existence-check scans."""
    src = Path("app/api/admin_requests_stream.py").read_text()
    assert 'status_code=404, detail="activity_log row not found"' in src


def test_endpoint_admin_gated():
    src = Path("app/api/admin_requests_stream.py").read_text()
    # require_admin dependency present in the function signature
    assert "_admin: AdminUser = Depends(require_admin)" in src
    # And on the detail endpoint specifically
    tail = src[src.find("async def request_detail"):]
    assert "require_admin" in tail[:400]


def test_response_shape_documented():
    src = Path("app/api/admin_requests_stream.py").read_text()
    # Docstring specifies the 5 top-level keys
    tail = src[src.find("async def request_detail"):]
    for key in ("row", "provider", "api_key", "correlated_events",
                "correlation_window_sec"):
        assert f'"{key}"' in tail, f"detail endpoint missing key: {key}"


def test_api_key_hash_is_redacted():
    """The full api_key_id is a SHA-256 prefix — leaking more than 8
    hex chars to the response adds no diagnostic value + increases
    surface if the JSON is logged elsewhere."""
    src = Path("app/api/admin_requests_stream.py").read_text()
    assert "def _redact_key_hash" in src
    assert "_REDACTED_KEY_LEN = 8" in src
    # And the summary uses the redacted form:
    assert '"id_prefix"' in src


def test_correlation_window_is_bounded():
    """The correlated_events lookup must have both a time window AND a
    row-count cap so a chatty key doesn't overwhelm the response."""
    src = Path("app/api/admin_requests_stream.py").read_text()
    assert "_CORRELATION_WINDOW_SEC = 30" in src
    assert "_CORRELATED_MAX_ROWS = 50" in src
    # And the query actually uses both:
    tail = src[src.find("async def _correlated_events"):]
    assert ".limit(_CORRELATED_MAX_ROWS)" in tail


def test_correlation_scopes_to_same_key_and_provider():
    """Correlation must be scoped — matching only rows sharing the same
    api_key_id (mandatory) AND provider_id (or provider-agnostic system
    events). Without this scope every activity_log row within 30s
    would return, defeating the point."""
    src = Path("app/api/admin_requests_stream.py").read_text()
    tail = src[src.find("async def _correlated_events"):]
    assert "ActivityLog.api_key_id == row.api_key_id" in tail
    assert "ActivityLog.provider_id == row.provider_id" in tail


def test_correlated_events_include_delta_ms():
    """Each correlated event needs a signed delta from the anchor row so
    the operator can see 'this happened 500ms after the request' at a
    glance."""
    src = Path("app/api/admin_requests_stream.py").read_text()
    assert '"delta_ms"' in src


def test_provider_summary_doesnt_leak_credentials():
    src = Path("app/api/admin_requests_stream.py").read_text()
    start = src.find("async def _provider_summary")
    # Bound the search to just this function body — next `\nasync def`
    # or `\n@router.` marks the boundary.
    rest = src[start:]
    next_def = rest.find("\nasync def ", 10)
    next_route = rest.find("\n@router.", 10)
    end = min(x for x in (next_def, next_route, len(rest)) if x > 0)
    body = rest[:end]
    # Explicitly-listed safe fields; NEVER include api_key or oauth
    # tokens in the summary bundle.
    safe_fields = ("id", "name", "provider_type", "enabled", "cost_class")
    for f in safe_fields:
        assert f'"{f}"' in body
    # Negative pins: dict-KEY strings that must NOT appear in this
    # function's body.
    for danger in ('"api_key"', '"oauth_refresh_token"',
                   '"oauth_expires_at"', '"codex_session_cookies"',
                   '"anthropic_session_cookies"', '"encrypted_key"'):
        assert danger not in body, f"provider summary leaks {danger}"


def test_apikey_summary_doesnt_leak_full_id():
    src = Path("app/api/admin_requests_stream.py").read_text()
    start = src.find("async def _apikey_summary")
    rest = src[start:]
    next_def = rest.find("\nasync def ", 10)
    next_route = rest.find("\n@router.", 10)
    end = min(x for x in (next_def, next_route, len(rest)) if x > 0)
    body = rest[:end]
    # Never emit the full key id — only the redacted prefix.
    # If the body includes `"id":`, it must be a redaction call, not the raw hash.
    assert '"id":' not in body or "id_prefix" in body
    assert "encrypted_key" not in body


def test_row_dict_helper_reused_from_stream_endpoint():
    """The detail response's ``row`` key uses the same _row_to_dict shape
    as the stream's SSE events. Consistency: operator's client can
    parse both with the same code."""
    src = Path("app/api/admin_requests_stream.py").read_text()
    # _row_to_dict exists (from v5.11.0) and is called from request_detail
    tail = src[src.find("async def request_detail"):]
    assert "_row_to_dict(row)" in tail


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 20, 5), (
        f"expected >= 5.20.5, got {major}.{minor}.{patch}"
    )
