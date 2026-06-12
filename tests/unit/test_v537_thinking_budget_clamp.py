"""v5.3.7 — Gemini thinking-budget clamp (empty-success fix).

Gemini 2.5 counts thinking tokens toward maxOutputTokens. The router
injects thinking.budget_tokens=8192 for google/vertex Gemini 2.5 routes;
when the caller's max_tokens <= that budget the model burns the entire
output cap on internal reasoning and returns HTTP 200 with empty content.
clamp_thinking_budget() shrinks the budget so content always has headroom.
"""
from app.routing.litellm_binding import (
    clamp_thinking_budget,
    _THINKING_BUDGET_MIN,
    _THINKING_CONTENT_HEADROOM,
)


def test_clamps_when_budget_exceeds_max_tokens():
    # The exact fleet failure: hub relays max_tokens=1024, router injects 8192.
    extra = {"max_tokens": 1024, "thinking": {"type": "enabled", "budget_tokens": 8192}}
    clamp_thinking_budget(extra)
    assert extra["thinking"]["budget_tokens"] == 512  # max_tokens // 2
    assert extra["thinking"]["budget_tokens"] + _THINKING_CONTENT_HEADROOM <= 8192


def test_clamps_at_hub_floor_4096():
    extra = {"max_tokens": 4096, "thinking": {"type": "enabled", "budget_tokens": 8192}}
    clamp_thinking_budget(extra)
    assert extra["thinking"]["budget_tokens"] == 2048


def test_no_clamp_when_headroom_exists():
    extra = {"max_tokens": 32768, "thinking": {"type": "enabled", "budget_tokens": 8192}}
    clamp_thinking_budget(extra)
    assert extra["thinking"]["budget_tokens"] == 8192  # untouched


def test_floor_is_gemini_pro_minimum():
    # Tiny max_tokens still leaves a valid budget (Pro 400s below 128).
    extra = {"max_tokens": 200, "thinking": {"type": "enabled", "budget_tokens": 8192}}
    clamp_thinking_budget(extra)
    assert extra["thinking"]["budget_tokens"] == _THINKING_BUDGET_MIN


def test_noop_without_thinking():
    extra = {"max_tokens": 1024}
    assert clamp_thinking_budget(extra) is extra
    assert "thinking" not in extra


def test_noop_without_max_tokens():
    extra = {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    clamp_thinking_budget(extra)
    assert extra["thinking"]["budget_tokens"] == 8192


def test_noop_on_malformed_thinking():
    # Anthropic client-forwarded thinking dicts may omit budget_tokens —
    # forward as-is, let the upstream surface its own validation error.
    extra = {"max_tokens": 1024, "thinking": {"type": "enabled"}}
    clamp_thinking_budget(extra)
    assert extra["thinking"] == {"type": "enabled"}

    extra2 = {"max_tokens": 1024, "thinking": "bogus"}
    clamp_thinking_budget(extra2)
    assert extra2["thinking"] == "bogus"


def test_never_raises_on_garbage():
    assert clamp_thinking_budget({}) == {}
    weird = {"max_tokens": -5, "thinking": {"budget_tokens": None}}
    assert clamp_thinking_budget(weird) is weird
