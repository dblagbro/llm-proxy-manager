"""v5.21.0 — Two shipped items in one release:

1. LMRH ``refuse-tolerance`` dim — per-request routing hint
   (strict/default/lenient) mapping to a safety floor/ceil range on
   provider profiles. Complements the existing per-provider
   ``refusal-rate`` dim.

2. Buffered-cascade streaming mode — when a request has
   ``stream=true`` AND the key has ``refusal_retry_enabled=true``,
   the proxy now runs the initial dispatch as non-streaming, executes
   the v5.20.1 cascade if refusal is detected, then converts the
   final ``anthropic_result`` to SSE frames. Replaces the v5.20.8
   ``X-Refusal-Cascade-Unavailable: streaming`` transparency header
   with a working ``X-Refusal-Cascade-Mode: buffered`` marker.
"""
from __future__ import annotations
from pathlib import Path


# ── refuse-tolerance dim ─────────────────────────────────────────────

def test_refuse_tolerance_dim_registered_in_weights():
    src = Path("app/routing/lmrh/types.py").read_text()
    assert '"refuse-tolerance"' in src
    # Weight should match the safety-max weight (8) since it's the same axis
    assert '"refuse-tolerance": 8' in src


def test_refuse_tolerance_taxonomy_is_three_valued():
    """Coarser vocabulary than refusal-rate (which has 4 values).
    Per operator's DevinGPT roadmap: strict / default / lenient."""
    src = Path("app/routing/lmrh/types.py").read_text()
    for v in ('"strict"', '"default"', '"lenient"'):
        assert v in src, f"missing refuse-tolerance value: {v}"


def test_refuse_tolerance_floor_ceil_maps_are_symmetric():
    """Both dicts must have the same keys — asymmetric maps would
    silently drop candidates for some values."""
    from app.routing.lmrh.types import (
        _REFUSE_TOLERANCE_TO_SAFETY_FLOOR,
        _REFUSE_TOLERANCE_TO_SAFETY_CEIL,
    )
    assert set(_REFUSE_TOLERANCE_TO_SAFETY_FLOOR) == set(_REFUSE_TOLERANCE_TO_SAFETY_CEIL)
    for k in _REFUSE_TOLERANCE_TO_SAFETY_FLOOR:
        assert _REFUSE_TOLERANCE_TO_SAFETY_FLOOR[k] <= _REFUSE_TOLERANCE_TO_SAFETY_CEIL[k], (
            f"floor > ceil for refuse-tolerance={k}"
        )


def test_refuse_tolerance_semantic_ordering():
    """strict should map to a higher safety range than lenient.
    (strict = wants MORE refusals; lenient = wants FEWER refusals.)"""
    from app.routing.lmrh.types import (
        _REFUSE_TOLERANCE_TO_SAFETY_FLOOR,
        _REFUSE_TOLERANCE_TO_SAFETY_CEIL,
    )
    strict_range = (
        _REFUSE_TOLERANCE_TO_SAFETY_FLOOR["strict"],
        _REFUSE_TOLERANCE_TO_SAFETY_CEIL["strict"],
    )
    lenient_range = (
        _REFUSE_TOLERANCE_TO_SAFETY_FLOOR["lenient"],
        _REFUSE_TOLERANCE_TO_SAFETY_CEIL["lenient"],
    )
    assert strict_range[0] > lenient_range[1], (
        f"strict range {strict_range} must be entirely above lenient range {lenient_range}"
    )


def test_refuse_tolerance_scoring_matches_provider_safety():
    """A provider with safety=4 should score for strict but not lenient."""
    from app.routing.lmrh.types import LMRHHint, HintDimension, CapabilityProfile
    from app.routing.lmrh.score import score_candidate

    prof = CapabilityProfile(
        provider_id="p1", provider_type="anthropic", model_id="claude-sonnet",
        safety=4,
    )

    strict_hint = LMRHHint(raw="", dimensions=[
        HintDimension(key="refuse-tolerance", value="strict"),
    ])
    lenient_hint = LMRHHint(raw="", dimensions=[
        HintDimension(key="refuse-tolerance", value="lenient"),
    ])

    strict_score, strict_unmet = score_candidate(prof, strict_hint)
    lenient_score, lenient_unmet = score_candidate(prof, lenient_hint)

    assert "refuse-tolerance" not in strict_unmet, (
        f"safety=4 should MATCH strict range; unmet={strict_unmet}"
    )
    assert "refuse-tolerance" in lenient_unmet, (
        f"safety=4 should NOT match lenient range; unmet={lenient_unmet}"
    )
    assert strict_score > lenient_score, (
        f"strict {strict_score} should score higher than lenient {lenient_score} for safety=4"
    )


def test_refuse_tolerance_required_hard_gates():
    """``refuse-tolerance=strict;require`` should hard-drop lenient providers."""
    from app.routing.lmrh.types import LMRHHint, HintDimension, CapabilityProfile
    from app.routing.lmrh.score import score_candidate

    lenient_prof = CapabilityProfile(
        provider_id="p1", provider_type="grok", model_id="grok-3", safety=1,
    )
    strict_required = LMRHHint(raw="", dimensions=[
        HintDimension(key="refuse-tolerance", value="strict", required=True),
    ])
    score, unmet = score_candidate(lenient_prof, strict_required)
    assert score == float("-inf"), (
        "required strict must hard-drop lenient providers, got score={score}"
    )


# ── buffered-cascade streaming ────────────────────────────────────────

def test_buffered_stream_mode_marker_wired():
    src = Path("app/api/messages.py").read_text()
    # v5.20.8 header replaced with the working marker
    assert '"X-Refusal-Cascade-Mode"' in src
    assert '"buffered"' in src


def test_v5208_transparency_header_removed():
    """Once cascade actually works on streaming, the ``Unavailable``
    header shouldn't appear anymore — it lied about the state."""
    src = Path("app/api/messages.py").read_text()
    assert '"X-Refusal-Cascade-Unavailable"' not in src


def test_stream_flag_forced_false_in_buffered_mode():
    """Buffered mode must force stream=False so the non-streaming
    dispatch runs (which is what invokes cascade)."""
    src = Path("app/api/messages.py").read_text()
    # The forcing pattern
    assert "_buffered_cascade_stream" in src
    assert "stream = False" in src


def test_sse_emission_at_return_point():
    """The final anthropic_result must be converted to SSE frames when
    buffered mode was active, else the caller gets JSON when they asked
    for stream."""
    src = Path("app/api/messages.py").read_text()
    # Look for the SSE conversion at the anthropic_result return point
    assert "if _buffered_cascade_stream:" in src
    assert "anthropic_text_sse" in src
    assert "anthropic_tool_sse" in src or "anthropic_tools_sse" in src
    assert 'media_type="text/event-stream"' in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 0), (
        f"expected >= 5.21.0, got {major}.{minor}.{patch}"
    )
