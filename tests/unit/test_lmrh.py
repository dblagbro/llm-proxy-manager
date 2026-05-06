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


# v3.0.68 — comma-tolerant legacy parser (DevinGPT 2026-05-06 spec/impl gap)
def test_parse_provider_hint_comma_list_preserved():
    """provider-hint=a,b,c parses as ONE dim with value 'a,b,c', not three
    pieces with two of them orphaned + emitting unknown-dim warnings."""
    h = parse_hint("provider-hint=claude-oauth,codex-oauth,anthropic-direct;require")
    assert h is not None
    assert len(h.dimensions) == 1
    d = h.dimensions[0]
    assert d.key == "provider-hint"
    assert d.value == "claude-oauth,codex-oauth,anthropic-direct"
    assert d.required is True


def test_parse_region_comma_list_preserved():
    h = parse_hint("region=us,ca;require")
    assert h is not None
    d = h.get("region")
    assert d is not None
    assert d.value == "us,ca"
    assert d.required is True


def test_parse_mixed_dims_with_comma_list():
    """task=...,exclude=a,b,c — two dims, the second multi-value."""
    h = parse_hint("task=reasoning, exclude=foo,bar,baz, cost=economy")
    assert h is not None
    assert len(h.dimensions) == 3
    assert h.get("task").value == "reasoning"
    assert h.get("exclude").value == "foo,bar,baz"
    assert h.get("cost").value == "economy"


def test_parse_orphaned_value_at_start_dropped():
    """Bare value with no key at start has nothing to merge into; drop it."""
    h = parse_hint("orphan, task=reasoning")
    assert h is not None
    assert len(h.dimensions) == 1
    assert h.get("task") is not None


# v3.0.70 — fallback-chain alias of provider-hint (paperless-ai-analyzer
# ships this on every call; pre-v3.0.70 the ;require constraint was
# silently dropped because the dim wasn't in the builtin set).
def test_fallback_chain_match_boosts_score():
    """fallback-chain=<name> on a matching provider gets the same boost
    as provider-hint=<name>."""
    p = CapabilityProfile(
        provider_id="p1", provider_type="claude-oauth", model_id="claude-sonnet-4-6",
        provider_name="Devin-Anthropic-Max-VG",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    hint_fc = parse_hint("fallback-chain=anthropic")
    score_fc, _ = score_candidate(p, hint_fc)
    hint_ph = parse_hint("provider-hint=anthropic")
    score_ph, _ = score_candidate(p, hint_ph)
    assert score_fc == score_ph


def test_fallback_chain_require_hard_filter_eliminates():
    """fallback-chain=anthropic;require on a non-anthropic profile must
    return -inf (was silently passing pre-v3.0.70)."""
    p_openai = CapabilityProfile(
        provider_id="p1", provider_type="openai", model_id="gpt-4o",
        provider_name="OpenAI-Direct",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    hint = parse_hint("fallback-chain=anthropic;require")
    score, _ = score_candidate(p_openai, hint)
    assert score == float("-inf")


def test_fallback_chain_require_passes_on_match():
    p = CapabilityProfile(
        provider_id="p1", provider_type="claude-oauth", model_id="claude-sonnet-4-6",
        provider_name="Devin-Anthropic-Max-VG",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    hint = parse_hint("fallback-chain=anthropic;require")
    score, _ = score_candidate(p, hint)
    assert score > 0


def test_fallback_chain_in_builtin_dim_names():
    """Eliminates the unknown-dim:fallback-chain warning that was
    being emitted on every paperless-ai-analyzer call."""
    from app.api.lmrh import _builtin_dim_names
    assert "fallback-chain" in _builtin_dim_names()


def test_fallback_chain_alongside_other_dims():
    """The exact composite hint paperless ships in production:
    task=analysis, cost=standard, safety-min=3, fallback-chain=anthropic;require"""
    h = parse_hint("task=analysis, cost=standard, safety-min=3, fallback-chain=anthropic;require")
    assert h is not None
    fc = h.get("fallback-chain")
    assert fc is not None
    assert fc.value == "anthropic"
    assert fc.required is True


# v3.0.70 — provider-family fuzzy match for provider-hint / fallback-chain
def test_provider_hint_strict_match_by_name_still_works():
    """Caller with the full provider_name still matches (back-compat)."""
    p = CapabilityProfile(
        provider_id="p1", provider_type="claude-oauth", model_id="claude-sonnet-4-6",
        provider_name="Devin-Anthropic-Max-VG",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    h = parse_hint("provider-hint=Devin-Anthropic-Max-VG")
    score, _ = score_candidate(p, h)
    assert score > 0


def test_provider_hint_strict_match_by_type_still_works():
    """Caller with the exact provider_type still matches (back-compat)."""
    p = CapabilityProfile(
        provider_id="p1", provider_type="claude-oauth", model_id="claude-sonnet-4-6",
        provider_name="Devin-Anthropic-Max-VG",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    h = parse_hint("provider-hint=claude-oauth")
    score, _ = score_candidate(p, h)
    assert score > 0


def test_provider_hint_family_match_anthropic_covers_claude_oauth():
    """provider-hint=anthropic now matches claude-oauth (family expansion).
    Pre-v3.0.70 this would have failed strict-equality against the type."""
    p = CapabilityProfile(
        provider_id="p1", provider_type="claude-oauth", model_id="claude-sonnet-4-6",
        provider_name="Devin-Anthropic-Max-VG",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    h = parse_hint("provider-hint=anthropic;require")
    score, _ = score_candidate(p, h)
    assert score > 0


def test_provider_hint_family_match_openai_covers_codex_oauth():
    p = CapabilityProfile(
        provider_id="p1", provider_type="codex-oauth", model_id="gpt-5.5",
        provider_name="Devin-Codex-Gmail",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    h = parse_hint("provider-hint=openai;require")
    score, _ = score_candidate(p, h)
    assert score > 0


def test_provider_hint_family_match_google_covers_vertex():
    p = CapabilityProfile(
        provider_id="p1", provider_type="vertex_ai", model_id="gemini-2.5-flash",
        provider_name="C1 Vertex AI",
        tasks=["analysis"], cost_tier="economy", priority=10,
    )
    h = parse_hint("provider-hint=google;require")
    score, _ = score_candidate(p, h)
    assert score > 0


def test_provider_hint_unknown_family_still_falls_through():
    """An unrecognized family token falls through to strict match —
    so it doesn't accidentally become permissive."""
    p = CapabilityProfile(
        provider_id="p1", provider_type="claude-oauth", model_id="claude-sonnet-4-6",
        provider_name="Devin-Anthropic-Max-VG",
        tasks=["analysis"], cost_tier="standard", priority=10,
    )
    h = parse_hint("provider-hint=banana;require")
    score, _ = score_candidate(p, h)
    assert score == float("-inf")
