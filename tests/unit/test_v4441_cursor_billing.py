"""v4.4.41 — Cursor dashboard usage scrape + multi-vendor preferred-pick.

Tests cover the four moving pieces:
  1. parse_usage_response mapping (Cursor JSON → ExternalUsageSnapshot fields)
  2. fetch_usage HTTP behavior (auth ok / 401 → session_expired / network err)
  3. evaluate_rules_for_all_providers now covers cursor-oauth alongside claude-oauth
  4. reorder_subscription_by_utilization (new) + reorder_claude_oauth_by_utilization
     (back-compat that now also handles cursor-oauth in one pass)

Background memory: project_backlog_cursor_oauth_usage_monitoring.md.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


# ── parse_usage_response ────────────────────────────────────────────


def test_parse_usage_response_maps_totalPercentUsed_to_seven_day_utilization():
    """The routing signal: ``individualUsage.plan.totalPercentUsed`` →
    ``seven_day_utilization``. Reusing the Anthropic-shaped column name
    is intentional — the SEMANTICS (current util %) generalize."""
    from app.providers.cursor_billing import parse_usage_response
    merged = {
        "auth_me": {"sub": "user_01J..."},
        "usage_summary": {
            "billingCycleStart": "2026-05-22T18:22:28.366Z",
            "billingCycleEnd": "2026-06-22T18:22:28.366Z",
            "membershipType": "pro",
            "individualUsage": {
                "plan": {
                    "totalPercentUsed": 47.3,
                    "autoPercentUsed": 50.0,
                    "apiPercentUsed": 12.0,
                    "used": 1000, "limit": 5000, "remaining": 4000,
                }
            }
        },
        "aggregated_events": {
            "totalInputTokens": "27581",
            "totalOutputTokens": "1468",
            "totalCacheReadTokens": "43777",
            "totalCostCents": 5.42285,
        },
    }
    out = parse_usage_response(merged)
    assert out["seven_day_utilization"] == 47.3
    # billingCycleEnd parsed to a datetime (naive UTC)
    assert isinstance(out["seven_day_resets_at"], datetime)
    assert out["seven_day_resets_at"].year == 2026
    assert out["seven_day_resets_at"].month == 6
    # cost mapping: cents → dollars
    assert out["extra_usage_used_credits"] == pytest.approx(5.42285 / 100.0)
    assert out["extra_usage_currency"] == "USD"


def test_parse_usage_response_tolerates_missing_individualUsage():
    """A free-tier or empty account may not have the plan block — we
    must not crash; just omit the routing-signal fields."""
    from app.providers.cursor_billing import parse_usage_response
    merged = {"auth_me": None, "usage_summary": {"membershipType": "free"}, "aggregated_events": {}}
    out = parse_usage_response(merged)
    assert "seven_day_utilization" not in out
    assert "seven_day_resets_at" not in out


def test_parse_usage_response_tolerates_missing_aggregated_events():
    from app.providers.cursor_billing import parse_usage_response
    merged = {
        "auth_me": None,
        "usage_summary": {
            "billingCycleEnd": "2026-06-22T18:22:28.366Z",
            "individualUsage": {"plan": {"totalPercentUsed": 12.0}},
        },
        "aggregated_events": None,
    }
    out = parse_usage_response(merged)
    assert out["seven_day_utilization"] == 12.0
    assert "extra_usage_used_credits" not in out


# ── fetch_usage HTTP behavior ───────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_usage_happy_path_sends_cookie_header():
    """Confirms the auth shape — Cookie: WorkosCursorSessionToken=…,
    NOT Authorization: Bearer. This is THE finding from the upstream
    routes/cursor.js inspection that made this whole module viable."""
    from app.providers import cursor_billing

    captured_headers = []

    class FakeResp:
        status_code = 200
        text = ""
        def json(self):
            # Return different shapes per path so we can assert the merge
            return {"sub": "user_01"} if "auth/me" in self._url else (
                {"individualUsage": {"plan": {"totalPercentUsed": 25.0}},
                 "billingCycleEnd": "2026-06-22T18:22:28.366Z"}
                if "usage-summary" in self._url else
                {"totalCostCents": 5.0}
            )

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None):
            captured_headers.append(dict(headers or {}))
            r = FakeResp(); r._url = url; return r

    with patch("app.providers.cursor_billing.httpx.AsyncClient", FakeClient):
        result = await cursor_billing.fetch_usage(cookie_value="user_xxx::eyJfake")

    assert result.ok is True
    assert result.auth_state == "ok"
    # Every call must have the cookie header, not a Bearer
    for h in captured_headers:
        assert h["Cookie"] == "WorkosCursorSessionToken=user_xxx::eyJfake"
        assert "Authorization" not in h
    # The parsed dict has all 3 endpoint keys
    assert set(result.parsed.keys()) == {"auth_me", "usage_summary", "aggregated_events"}


@pytest.mark.asyncio
async def test_fetch_usage_401_returns_session_expired():
    from app.providers import cursor_billing
    import urllib.error

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
            self.text = "auth fail"
        def json(self): return {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None):
            return FakeResp(401)

    with patch("app.providers.cursor_billing.httpx.AsyncClient", FakeClient):
        result = await cursor_billing.fetch_usage(cookie_value="stale")
    assert result.ok is False
    assert result.auth_state == "session_expired"
    assert "re-authorize" in result.error.lower()


# ── evaluate_rules_for_all_providers covers both types ──────────────


def test_evaluate_rules_query_includes_cursor_oauth():
    """The auto-skip evaluator's "evaluate all" sweep must cover
    cursor-oauth too — otherwise the cron-driven snapshot writes will
    update ExternalUsageSnapshot rows but auto_skip_until never fires
    for cursor providers."""
    from pathlib import Path
    src = Path("app/routing/external_rotation.py").read_text()
    # The query must list cursor-oauth explicitly (or via a tuple/.in_())
    idx = src.index("evaluate_rules_for_all_providers")
    block = src[idx:idx + 800]
    assert "cursor-oauth" in block, (
        "evaluate_rules_for_all_providers must include cursor-oauth in its "
        "provider_type filter — otherwise the auto-skip cron only ever "
        "rotates claude-oauth providers."
    )


# ── reorder_subscription_by_utilization ────────────────────────────


class _FakeProvider:
    def __init__(self, id, provider_type, priority):
        self.id = id
        self.provider_type = provider_type
        self.priority = priority


def test_reorder_subscription_by_utilization_claude_only():
    """v4.4.41 new generalized function. With provider_type='claude-oauth',
    behavior matches the v3.7.4 original."""
    from app.routing.external_rotation import reorder_subscription_by_utilization
    a = _FakeProvider("a", "claude-oauth", priority=5)  # util 80%
    b = _FakeProvider("b", "claude-oauth", priority=5)  # util 20% — should sort first
    c = _FakeProvider("c", "openai", priority=5)        # untouched
    util_map = {"a": 80.0, "b": 20.0}
    result = reorder_subscription_by_utilization(
        [a, b, c], util_map, provider_type="claude-oauth",
    )
    # b (lower util) should now be at the first claude-oauth position
    assert result[0] is b
    assert result[1] is a
    assert result[2] is c  # openai untouched


def test_reorder_subscription_by_utilization_cursor():
    """Same logic for cursor-oauth — confirms the type parameter actually
    gates the filter."""
    from app.routing.external_rotation import reorder_subscription_by_utilization
    a = _FakeProvider("a", "cursor-oauth", priority=5)  # util 80
    b = _FakeProvider("b", "cursor-oauth", priority=5)  # util 20
    result = reorder_subscription_by_utilization(
        [a, b], {"a": 80.0, "b": 20.0}, provider_type="cursor-oauth",
    )
    assert result[0] is b
    assert result[1] is a


def test_reorder_claude_oauth_by_utilization_now_handles_both():
    """v4.4.41: the back-compat ``reorder_claude_oauth_by_utilization``
    name now reorders BOTH claude-oauth AND cursor-oauth in one pass.
    Each subscription type stays scoped to its own subset."""
    from app.routing.external_rotation import reorder_claude_oauth_by_utilization
    ca = _FakeProvider("ca", "claude-oauth", priority=5)  # util 80
    cb = _FakeProvider("cb", "claude-oauth", priority=5)  # util 20
    cura = _FakeProvider("cura", "cursor-oauth", priority=5)  # util 90
    curb = _FakeProvider("curb", "cursor-oauth", priority=5)  # util 10
    others = _FakeProvider("o", "openai", priority=5)

    util_map = {"ca": 80.0, "cb": 20.0, "cura": 90.0, "curb": 10.0}
    result = reorder_claude_oauth_by_utilization(
        [ca, cb, cura, curb, others], util_map,
    )
    # claude-oauth slots: positions 0 and 1 — cb wins
    assert result[0] is cb
    assert result[1] is ca
    # cursor-oauth slots: positions 2 and 3 — curb wins
    assert result[2] is curb
    assert result[3] is cura
    # other (openai) untouched at position 4
    assert result[4] is others


# ── main.py wires the cursor worker on startup ─────────────────────


def test_main_starts_cursor_billing_worker():
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert "cursor_billing_worker" in src
    # Same try/except shape as the Anthropic and Codex workers
    assert "cursor_billing_worker" in src and "_cur_worker.start()" in src
