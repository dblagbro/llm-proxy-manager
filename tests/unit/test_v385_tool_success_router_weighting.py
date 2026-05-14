"""v3.8.5 (#265) — tool-call success weighting in router scoring.

When `has_tools=True`, the router multiplies candidate scores by their
rolling tool-call success rate (from the v3.8.4 prober) and hard-skips
candidates with rate < 0.3. Candidates without probe data are unchanged.

Closes the last item of the 4-part tool-simulation audit.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── Schema additions ───────────────────────────────────────────────


def test_model_capability_has_tool_call_success_rate_column():
    from app.models.db import ModelCapability
    cols = {c.name for c in ModelCapability.__table__.columns}
    assert "tool_call_success_rate" in cols


def test_migration_adds_tool_call_success_rate_column():
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE model_capabilities ADD COLUMN tool_call_success_rate REAL" in src


def test_capability_profile_carries_tool_call_success_rate():
    from app.routing.lmrh.types import CapabilityProfile
    import dataclasses
    fields = {f.name for f in dataclasses.fields(CapabilityProfile)}
    assert "tool_call_success_rate" in fields
    # Default must be None (= no probe data yet)
    p = CapabilityProfile(
        provider_id="p", provider_type="openai", model_id="m",
    )
    assert p.tool_call_success_rate is None


# ── Router scoring helper ──────────────────────────────────────────


def test_apply_weighting_no_change_when_rate_is_none():
    from app.routing.router import _apply_tool_success_weighting
    from app.routing.lmrh.types import CapabilityProfile
    p = CapabilityProfile(provider_id="p", provider_type="x", model_id="m")
    p.tool_call_success_rate = None
    out = _apply_tool_success_weighting([(p, set(), 5.0)])
    assert out[0][2] == 5.0  # unchanged


def test_apply_weighting_multiplies_score_by_rate():
    from app.routing.router import _apply_tool_success_weighting
    from app.routing.lmrh.types import CapabilityProfile
    p = CapabilityProfile(provider_id="p", provider_type="x", model_id="m")
    p.tool_call_success_rate = 0.8
    out = _apply_tool_success_weighting([(p, set(), 10.0)])
    assert out[0][2] == pytest.approx(8.0)


def test_apply_weighting_hard_skips_low_rate():
    from app.routing.router import _apply_tool_success_weighting, _TOOL_SUCCESS_HARD_SKIP_THRESHOLD
    from app.routing.lmrh.types import CapabilityProfile
    p = CapabilityProfile(provider_id="p", provider_type="x", model_id="m")
    p.tool_call_success_rate = _TOOL_SUCCESS_HARD_SKIP_THRESHOLD - 0.05  # below threshold
    out = _apply_tool_success_weighting([(p, set(), 10.0)])
    assert out[0][2] == float("-inf")


def test_apply_weighting_preserves_ordering_among_equal_rates():
    """Two candidates with the same success rate keep their relative
    score ordering — weighting is multiplicative and proportional."""
    from app.routing.router import _apply_tool_success_weighting
    from app.routing.lmrh.types import CapabilityProfile
    p1 = CapabilityProfile(provider_id="p1", provider_type="x", model_id="m")
    p1.tool_call_success_rate = 0.9
    p2 = CapabilityProfile(provider_id="p2", provider_type="x", model_id="m")
    p2.tool_call_success_rate = 0.9
    out = _apply_tool_success_weighting([(p1, set(), 10.0), (p2, set(), 5.0)])
    # Both multiplied by 0.9 — relative ordering unchanged
    assert out[0][2] > out[1][2]


def test_apply_weighting_handles_garbage_rate():
    """Defensive: a non-numeric rate doesn't crash the router."""
    from app.routing.router import _apply_tool_success_weighting
    from app.routing.lmrh.types import CapabilityProfile
    p = CapabilityProfile(provider_id="p", provider_type="x", model_id="m")
    p.tool_call_success_rate = "not-a-float"  # type: ignore[assignment]
    out = _apply_tool_success_weighting([(p, set(), 10.0)])
    # Falls through to "no change" rather than raising
    assert out[0][2] == 10.0


# ── Wired into router build path ───────────────────────────────────


def test_router_applies_weighting_when_has_tools():
    """Source-level check: the weighting helper is called only when
    has_tools is true."""
    src = Path("app/routing/router.py").read_text()
    assert "_apply_tool_success_weighting" in src
    # The call site is gated on has_tools
    idx = src.index("if has_tools and ranked_scored:")
    body = src[idx:idx + 2000]
    assert "_apply_tool_success_weighting(ranked_scored)" in body


def test_router_re_sorts_after_weighting():
    """Weighting changes scores; re-sort + filter -inf candidates
    so they don't pollute downstream P2C / fallback chain selection."""
    src = Path("app/routing/router.py").read_text()
    idx = src.index("if has_tools and ranked_scored:")
    body = src[idx:idx + 2000]
    assert "ranked_scored.sort" in body
    assert 'float("-inf")' in body


def test_router_raises_when_all_candidates_excluded():
    """If every candidate has rate < 0.3, raise rather than silently
    routing to a known-broken provider."""
    src = Path("app/routing/router.py").read_text()
    idx = src.index("if has_tools and ranked_scored:")
    body = src[idx:idx + 2000]
    assert "All candidates excluded" in body


# ── Prober writes the rate ─────────────────────────────────────────


def test_prober_writes_tool_call_success_rate():
    """update_native_tools_from_rolling must ALWAYS persist the rate
    even when the bool flag doesn't flip (hysteresis band) — otherwise
    the router weighting has stale data while the prober is in the
    no-change zone."""
    src = Path("app/monitoring/tool_capability_prober.py").read_text()
    assert "cap.tool_call_success_rate" in src
    # The writes happen unconditionally on the capability rows (not
    # gated on new_val being non-None)
    idx = src.index("def update_native_tools_from_rolling")
    body = src[idx:idx + 3000]
    assert "cap.tool_call_success_rate = float(rate)" in body


def test_capability_profile_built_with_success_rate():
    """The router's profile-build path reads the column."""
    src = Path("app/routing/router.py").read_text()
    assert "tool_call_success_rate=getattr(cap" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 5)
