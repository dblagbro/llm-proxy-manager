"""v5.9.1 — regression test for the keepalive probe auth-failure streak.

v5.8.3 fix #3 routes probe auth failures through ``record_failure``
(not ``record_auth_failure``) so a single transient 401 doesn't auto-skip
the provider for 24h. The trade-off: when a refresh_token is permanently
revoked and the auto_skip_until set by an organic 401 eventually expires,
probes resume and the CB cycles indefinitely (open → 120s → reopen at
failures+1 → repeat) generating one ``circuit_breaker.opened`` warning
per sweep.

v5.9.1 adds a per-provider consecutive auth-failure streak counter; once
it crosses ``_PROBE_AUTH_FAILURE_RE_SKIP_THRESHOLD`` we re-persist
auto_skip_until=+24h so the v5.8.6/v5.8.7 gates re-engage.
"""
from __future__ import annotations

import inspect


def test_streak_dict_exists():
    from app.monitoring import keepalive as mod
    assert hasattr(mod, "_PROBE_AUTH_FAILURE_STREAK"), (
        "v5.9.1 introduced _PROBE_AUTH_FAILURE_STREAK; the dict must "
        "exist as a module-level singleton."
    )
    assert isinstance(mod._PROBE_AUTH_FAILURE_STREAK, dict)


def test_threshold_is_reasonable():
    from app.monitoring import keepalive as mod
    assert hasattr(mod, "_PROBE_AUTH_FAILURE_RE_SKIP_THRESHOLD")
    # 1 is too low (single transient 401 re-skips); 100 means 8+ hours
    # of log spam before the gate re-engages. 5–20 is the right range.
    assert 5 <= mod._PROBE_AUTH_FAILURE_RE_SKIP_THRESHOLD <= 20


def test_auth_failure_classifier_matches_canonical_phrases():
    from app.monitoring.keepalive import _looks_like_auth_failure
    # Positives — the actual phrases keepalive sees from production.
    for s in [
        "401 Unauthorized",
        "Invalid authentication credentials",
        "OAuthFlowError('Refresh failed (401)')",
        "invalid_grant",
        "refresh_token_reused",
        "Missing scopes: model.request",
        "needs_reauth",
    ]:
        assert _looks_like_auth_failure(s), (
            f"_looks_like_auth_failure should match {s!r}"
        )
    # Negatives — these are CB-tripping but not auth-class.
    for s in [
        "429 Rate limit",
        "503 Service Unavailable",
        "TimeoutException: deadline",
        "",
    ]:
        assert not _looks_like_auth_failure(s), (
            f"_looks_like_auth_failure should NOT match {s!r}"
        )


def test_probe_one_calls_persist_auto_skip_on_streak():
    """Sanity: the source of _probe_one references _persist_auto_skip
    on the streak path. This is the contract — the actual call site is
    integration-tested via the live smoke instance."""
    from app.monitoring import keepalive as mod
    src = inspect.getsource(mod._probe_one)
    assert "_persist_auto_skip" in src
    assert "_PROBE_AUTH_FAILURE_STREAK" in src
    assert "persistent_auth_failure_via_probe_streak" in src
