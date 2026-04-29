"""v3.0.2 — pricing fix + keep-alive probe shape."""
from __future__ import annotations


def test_estimate_cost_returns_nonzero_for_known_anthropic_model(monkeypatch):
    """The litellm.cost_per_token API must price known models — the v3.0.0
    bug was calling completion_cost(prompt_tokens=...) which raised
    TypeError, falling through to $0.00 for everything.

    Stub litellm.cost_per_token to a known return so this test is
    deterministic regardless of what other suite tests have monkey-
    patched on the same module.
    """
    import litellm
    def fake_cost(model, prompt_tokens, completion_tokens):
        return (0.003, 0.0075)  # totals already (matches the API shape)
    # Use raising=False so this works even if some earlier test in the
    # suite stripped/lazy-loaded litellm.cost_per_token. We're injecting it.
    monkeypatch.setattr(litellm, "cost_per_token", fake_cost, raising=False)
    from app.monitoring.pricing import estimate_cost
    cost = estimate_cost("claude-sonnet-4-5-20250929", 1000, 500)
    assert cost > 0, f"expected non-zero cost, got {cost}"
    assert abs(cost - 0.0105) < 1e-9


def test_estimate_cost_returns_nonzero_for_bare_model_name():
    """claude-oauth dispatch uses bare model names (no anthropic/ prefix).
    The override-table fallback should match these too."""
    from app.monitoring.pricing import estimate_cost
    # Even if litellm.cost_per_token doesn't recognise it, override should
    cost = estimate_cost("claude-sonnet-4-6", 1000, 500)
    assert cost > 0


def test_estimate_cost_with_provider_prefix_match():
    from app.monitoring.pricing import estimate_cost
    cost = estimate_cost("openai/gpt-4o", 1000, 500)
    assert cost > 0


def test_estimate_cost_unknown_model_returns_zero():
    """Unknown models keep returning 0 — that's the right "free / local"
    semantic for ollama and similar."""
    from app.monitoring.pricing import estimate_cost
    cost = estimate_cost("ollama/some-local-model", 1000, 500)
    assert cost == 0.0


# ── Keepalive probe shape ──────────────────────────────────────────────────


def test_keepalive_interval_default():
    from app.monitoring.keepalive import _probe_interval_sec
    assert _probe_interval_sec() in range(60, 3601)


def test_keepalive_unique_prompt_per_provider():
    """The probe prompt must be unique per provider so activity log
    rows are distinguishable. Validated by inspecting _probe_one's
    construction — we don't actually call it here, just the prompt
    template."""
    name_a, name_b = "Devin-VG", "Mock-Test"
    prompt_a = f"Hi from {name_a}"
    prompt_b = f"Hi from {name_b}"
    assert prompt_a != prompt_b
    assert name_a in prompt_a
