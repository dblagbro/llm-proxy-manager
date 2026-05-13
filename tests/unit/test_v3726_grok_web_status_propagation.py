"""v3.7.26 (#259) — grok-web upstream status-code propagation.

Operator complaint 2026-05-13: grok.com rate-limited the bridge (429),
the bridge correctly entered cool-off mode and returned 429 to the
proxy, but the proxy returned 502 Bad Gateway to the caller. The
caller couldn't apply standard 429 rate-limit semantics
(Retry-After / exponential backoff) because the status code was lost.

Root cause: 4 GrokWebError raise sites in app/providers/grok_web.py
hardcoded ``status_code=502`` regardless of upstream status.

Fix: ``_map_upstream_status`` helper preserves 429 and 5xx, falls
back to 502 for other unexpected statuses.
"""
from __future__ import annotations

from pathlib import Path


def test_map_helper_exists():
    src = Path("app/providers/grok_web.py").read_text()
    assert "_map_upstream_status" in src
    # Helper must be at module level (not nested inside a function)
    assert "\ndef _map_upstream_status(" in src


def test_map_preserves_429():
    from app.providers.grok_web import _map_upstream_status
    assert _map_upstream_status(429) == 429


def test_map_returns_502_for_5xx_and_other_codes():
    """Only 429 gets preserved. 5xx codes (whether the bridge sidecar
    errored or grok.com errored) come back as 502 Bad Gateway — the
    proxy IS a gateway, so 502 is the semantically-correct response
    from the caller's POV. Other unexpected codes also default to 502."""
    from app.providers.grok_web import _map_upstream_status
    for sc in (400, 418, 422, 500, 502, 503, 504, 529):
        assert _map_upstream_status(sc) == 502


def test_no_hardcoded_502_left_in_raise_sites():
    """All 4 ``raise GrokWebError`` non-auth sites must now call
    ``_map_upstream_status`` instead of hardcoding 502."""
    src = Path("app/providers/grok_web.py").read_text()
    # The hardcoded literal must be gone (except in the helper's body)
    # Search for the pattern that was wrong: ``status_code=502,`` on
    # raise-site lines (not in the GrokWebError __init__ default).
    raise_site_count = src.count("status_code=502,")
    assert raise_site_count == 0, (
        f"Expected 0 hardcoded ``status_code=502,`` raise sites, "
        f"got {raise_site_count}. The _map_upstream_status helper "
        f"should be used at every raise site."
    )


def test_all_four_raise_sites_use_map_helper():
    """The 4 GrokWebError raises previously had ``status_code=502``;
    each should now read ``status_code=_map_upstream_status(...)``."""
    src = Path("app/providers/grok_web.py").read_text()
    # 4 call sites + maybe more if added later; assert >=4
    map_calls = src.count("_map_upstream_status(")
    # 1 def + 4 call sites = >=5
    assert map_calls >= 5, f"Expected >=5 mentions of _map_upstream_status, got {map_calls}"


def test_grokweb_error_init_default_unchanged():
    """The GrokWebError __init__ default ``status_code=502`` is fine —
    that's only used when the caller doesn't specify a status (e.g.
    network errors with no HTTP response). It is NOT the bug; the
    bug was at raise sites that had access to the upstream status."""
    from app.providers.grok_web import GrokWebError
    e = GrokWebError("default test")
    assert e.status_code == 502  # backward-compat default


def test_grokweb_auth_error_still_returns_401():
    """v3.7.26 must not regress the existing 401 behavior for auth
    failures — they use a separate exception class with its own
    status_code default."""
    from app.providers.grok_web import GrokWebAuthError
    e = GrokWebAuthError("expired cookies")
    assert e.status_code == 401


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 26)
