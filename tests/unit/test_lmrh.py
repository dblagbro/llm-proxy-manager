"""Unit tests for the LMRH scoring engine."""
import pytest
from app.routing.lmrh import (
    parse_hint, rank_candidates, score_candidate, CapabilityProfile, LMRHHint
)


def _profile(pid, tasks, cost, latency, safety=3, native=False, priority=10):
    return CapabilityProfile(
        provider_id=pid, provider_type="openai", model_id="test",
        tasks=tasks, cost_tier=cost, latency=latency, safety=safety,
        native_reasoning=native, priority=priority,
    )


def test_parse_hint_basic():
    h = parse_hint("task=reasoning, cost=economy, region=us")
    assert h is not None
    assert h.get("task").value == "reasoning"
    assert h.get("cost").value == "economy"
    assert h.get("region").value == "us"


def test_parse_hint_required():
    h = parse_hint("task=reasoning, safety-min=4;require")
    assert h.get("safety-min").required is True
    assert h.get("task").required is False


def test_parse_hint_none_on_empty():
    assert parse_hint("") is None
    assert parse_hint(None) is None


def test_score_task_match():
    profile = _profile("p1", ["reasoning", "code"], "standard", "medium")
    hint = parse_hint("task=reasoning")
    score, unmet = score_candidate(profile, hint)
    assert score > 0
    assert "task" not in unmet


def test_score_task_mismatch_soft():
    profile = _profile("p1", ["chat"], "standard", "medium")
    hint = parse_hint("task=reasoning")
    score, unmet = score_candidate(profile, hint)
    assert "task" in unmet


def test_hard_constraint_fails():
    profile = _profile("p1", ["chat"], "standard", "medium", safety=2)
    hint = parse_hint("safety-min=4;require")
    score, unmet = score_candidate(profile, hint)
    assert score == float("-inf")


def test_rank_candidates_priority_order():
    p1 = _profile("high", ["chat"], "economy", "low", priority=1)
    p2 = _profile("low", ["chat"], "economy", "low", priority=10)
    ranked = rank_candidates([p2, p1], None)
    assert ranked[0][0].provider_id == "high"


def test_rank_excludes_hard_failures():
    p1 = _profile("safe", ["reasoning"], "standard", "medium", safety=4)
    p2 = _profile("unsafe", ["reasoning"], "standard", "medium", safety=1)
    hint = parse_hint("safety-min=3;require")
    ranked = rank_candidates([p1, p2], hint)
    assert len(ranked) == 1
    assert ranked[0][0].provider_id == "safe"


def test_cost_routing():
    economy = _profile("cheap", ["chat"], "economy", "low", priority=5)
    premium = _profile("expensive", ["chat"], "premium", "medium", priority=5)
    hint = parse_hint("cost=economy")
    ranked = rank_candidates([economy, premium], hint)
    assert ranked[0][0].provider_id == "cheap"


# ── Wave 4 #18 — parser robustness (legacy fallback runs when http-sfv absent) ──

def test_parse_hint_whitespace_tolerant():
    """Parser should tolerate arbitrary whitespace around = and ,."""
    hint = parse_hint("  task = reasoning ,  safety-min = 3 ; require  ")
    assert hint is not None
    assert any(d.key == "task" and d.value == "reasoning" for d in hint.dimensions)
    assert any(d.key == "safety-min" and d.required for d in hint.dimensions)


def test_parse_hint_missing_equals_skipped():
    hint = parse_hint("task=reasoning,broken-no-equals,cost=economy")
    assert hint is not None
    keys = [d.key for d in hint.dimensions]
    assert keys.count("task") == 1 and keys.count("cost") == 1


def test_parse_hint_returns_none_when_no_valid_dims():
    assert parse_hint(",,,  ,,") is None


# v3.0.51 — LMRH 1.2 §E3 region matching: comma-separated + hierarchy
def _region_profile(regions):
    return CapabilityProfile(
        provider_id="p1", provider_type="vertex", model_id="test",
        tasks=["chat"], regions=regions, priority=10,
    )


def test_region_exact_match_soft():
    profile = _region_profile(["us"])
    hint = parse_hint("region=us")
    score, unmet = score_candidate(profile, hint)
    assert score > 0 and "region" not in unmet


def test_region_hierarchy_eu_matches_eu_west():
    profile = _region_profile(["eu-west"])
    hint = parse_hint("region=eu")
    score, unmet = score_candidate(profile, hint)
    assert score > 0 and "region" not in unmet


def test_region_require_hard_filter_eliminates():
    profile = _region_profile(["us"])
    hint = parse_hint("region=eu;require")
    score, _ = score_candidate(profile, hint)
    assert score == float("-inf")


def test_region_require_passes_via_hierarchy():
    profile = _region_profile(["eu-central"])
    hint = parse_hint("region=eu;require")
    score, _ = score_candidate(profile, hint)
    assert score > 0


def test_region_unconfigured_profile_passes():
    # Profile with empty regions list — backwards compat soft-pass
    profile = _region_profile([])
    hint = parse_hint("region=us;require")
    score, _ = score_candidate(profile, hint)
    assert score > 0


def test_region_any_token_always_matches():
    profile = _region_profile(["asia-east"])
    hint = parse_hint("region=any")
    score, _ = score_candidate(profile, hint)
    assert score > 0


# v3.0.52 — LMRH 1.2 §E3 ;sovereign modifier + disclosure headers
def test_sovereign_parsed_implies_required():
    h = parse_hint("region=eu-central;sovereign")
    d = h.get("region")
    assert d.sovereign is True and d.required is True


def test_sovereign_rejects_unconfigured_profile():
    profile = _region_profile([])
    hint = parse_hint("region=eu;sovereign")
    score, _ = score_candidate(profile, hint)
    assert score == float("-inf")


def test_sovereign_passes_with_explicit_region():
    profile = _region_profile(["eu-west"])
    hint = parse_hint("region=eu;sovereign")
    score, _ = score_candidate(profile, hint)
    assert score > 0


def test_capability_header_strict_region():
    from app.routing.lmrh import build_capability_header
    profile = _region_profile(["us"])
    hint = parse_hint("region=us")
    header = build_capability_header(profile, unmet=[], hint=hint)
    assert "served-region=us" in header
    assert "region-honored=strict" in header


def test_capability_header_loose_region_via_hierarchy():
    from app.routing.lmrh import build_capability_header
    profile = _region_profile(["eu-west"])
    hint = parse_hint("region=eu")
    header = build_capability_header(profile, unmet=[], hint=hint)
    assert "served-region=eu-west" in header
    assert "region-honored=loose" in header


def test_capability_header_omits_region_disclosure_when_no_hint():
    from app.routing.lmrh import build_capability_header
    profile = _region_profile(["us"])
    header = build_capability_header(profile, unmet=[])
    assert "served-region" not in header
    assert "region-honored" not in header
