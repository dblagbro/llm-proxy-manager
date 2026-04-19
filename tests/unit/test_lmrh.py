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
    premium = _profile("expensive", ["chat"], "premium", "medium", priority=1)
    hint = parse_hint("cost=economy")
    ranked = rank_candidates([economy, premium], hint)
    assert ranked[0][0].provider_id == "cheap"
