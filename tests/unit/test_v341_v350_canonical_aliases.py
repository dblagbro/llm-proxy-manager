"""v3.4.1 + v3.5.0 — canonical model identity tests.

Covers the two-step rollout that closed the /v1/models duplication:

  v3.4.1 — aliases column on ModelCapability + matches_capability +
           /v1/models de-dupes across canonical + alias spellings.
  v3.5.0 — LMRHv2.1: family/variant exposed on /lmrh/providers and SDK.
"""
from __future__ import annotations

import pytest


# ── canonical helpers ────────────────────────────────────────────────


def test_matches_capability_exact():
    from app.routing.canonical import matches_capability
    assert matches_capability("x-ai/grok-3", "x-ai/grok-3", []) is True
    assert matches_capability("x-ai/grok-3", "claude-sonnet-4-6", []) is False


def test_matches_capability_via_alias():
    """v3.4.1 win: caller sends bare ``grok-3``, capability is canonical
    ``x-ai/grok-3`` with bare as an alias → matches."""
    from app.routing.canonical import matches_capability
    assert matches_capability(
        "grok-3", "x-ai/grok-3", ["grok-3"],
    ) is True


def test_matches_capability_case_insensitive():
    """Operators paste cURL strings; case varies."""
    from app.routing.canonical import matches_capability
    assert matches_capability("GROK-3", "x-ai/grok-3", ["grok-3"])
    assert matches_capability("X-AI/GROK-3", "x-ai/grok-3", []) is True


def test_matches_capability_empty_inputs():
    """Defensive: blank requested or blank model shouldn't crash."""
    from app.routing.canonical import matches_capability
    assert matches_capability("", "x-ai/grok-3", []) is False
    assert matches_capability("grok-3", "", ["grok-3"]) is True  # alias still wins


def test_derive_family_strips_one_prefix():
    from app.routing.canonical import derive_family
    assert derive_family("x-ai/grok-3") == "grok-3"
    assert derive_family("openai/gpt-4o") == "gpt-4o"
    # Bare name has no prefix to strip
    assert derive_family("claude-sonnet-4-6") == "claude-sonnet-4-6"
    # Multiple slashes — only strip the first segment
    assert derive_family("vendor/family/variant") == "family/variant"
    assert derive_family("") == ""
    assert derive_family(None) == ""  # type: ignore[arg-type]


def test_collect_canonical_aliases_orders_canonical_first():
    from app.routing.canonical import collect_canonical_aliases
    out = collect_canonical_aliases("x-ai/grok-3", ["grok-3", "Grok-3"])
    # Canonical first
    assert out[0] == "x-ai/grok-3"
    # Case-insensitive de-dupe — only one of "grok-3"/"Grok-3" remains
    assert "grok-3" in out
    assert len(out) == 2  # canonical + one bare


def test_collect_canonical_aliases_handles_no_aliases():
    from app.routing.canonical import collect_canonical_aliases
    assert collect_canonical_aliases("openai/gpt-4o", None) == ["openai/gpt-4o"]
    assert collect_canonical_aliases("openai/gpt-4o", []) == ["openai/gpt-4o"]


# ── grok_web alias declaration ────────────────────────────────────────


def test_grok_web_supported_models_canonical_only():
    """v3.4.1: SUPPORTED_MODELS is now canonical-only (no bare names);
    bare names are in SUPPORTED_MODEL_ALIASES instead. Pre-fix the list
    duplicated each model under bare + prefixed spellings, which is
    the root cause of the /v1/models duplication."""
    from app.providers.grok_web import SUPPORTED_MODELS, SUPPORTED_MODEL_ALIASES
    # Canonical list is OpenRouter-prefixed only
    assert SUPPORTED_MODELS == ["x-ai/grok-3", "x-ai/grok-4"]
    # Aliases map points each canonical → its bare alternate
    assert "grok-3" in SUPPORTED_MODEL_ALIASES["x-ai/grok-3"]
    assert "grok-4" in SUPPORTED_MODEL_ALIASES["x-ai/grok-4"]


def test_grok_web_alias_resolves_via_matcher():
    """End-to-end: caller sends ``grok-3``, the canonical capability is
    ``x-ai/grok-3``, the matcher resolves it. This is the test that
    proves the v3.4.1 fix didn't break the v3.2.8 use case (callers
    that paste OpenRouter slugs still work)."""
    from app.routing.canonical import matches_capability
    from app.providers.grok_web import SUPPORTED_MODELS, SUPPORTED_MODEL_ALIASES

    canonical = SUPPORTED_MODELS[0]  # "x-ai/grok-3"
    aliases = SUPPORTED_MODEL_ALIASES[canonical]

    # Both spellings resolve
    assert matches_capability("x-ai/grok-3", canonical, aliases)
    assert matches_capability("grok-3", canonical, aliases)
    # Wrong model doesn't
    assert not matches_capability("claude-sonnet-4-6", canonical, aliases)


# ── SDK ModelEntry exposes v2.1 fields ────────────────────────────────


def test_sdk_modelentry_carries_aliases_family_variant():
    """SDK ModelEntry has the new fields with empty/None defaults."""
    from sdk.python.lmrh_client import ModelEntry, ModelMetrics
    m = ModelEntry(
        model_id="x-ai/grok-3", kind="chat", context_length=128000,
        native_tools=False, native_reasoning=False,
        metrics=ModelMetrics(
            cost_per_1m_input_usd=None, cost_per_1m_output_usd=None,
            rated_quota_per_1m_input_usd=None,
            latency_p50_ms=None, latency_p95_ms=None,
            ttft_p50_ms=None, ttft_p95_ms=None,
            success_rate=None, samples=0,
        ),
    )
    assert m.aliases == ()
    assert m.family is None
    assert m.variant is None


def test_sdk_parses_v21_fields_when_proxy_emits_them():
    """Wire-format → typed conversion picks up v2.1 fields."""
    from sdk.python.lmrh_client import _snapshot_from_dict
    wire = {
        "version": "2.1",
        "as_of": "2026-05-09T20:00:00+00:00",
        "window_sec": 3600,
        "providers": [
            {
                "id": "p1", "name": "Grok-Web", "type": "grok-web",
                "priority": 1, "cost_class": "subscription",
                "circuit": "closed", "regions": [],
                "models": [
                    {
                        "model_id": "x-ai/grok-3", "kind": "chat",
                        "context_length": 128000,
                        "native_tools": False, "native_reasoning": False,
                        "aliases": ["grok-3"],
                        "family": "grok-3",
                        "variant": "web",
                        "metrics": {
                            "cost_per_1m_input_usd": None,
                            "cost_per_1m_output_usd": None,
                            "rated_quota_per_1m_input_usd": None,
                            "latency_p50_ms": 2500.0,
                            "latency_p95_ms": 6800.0,
                            "ttft_p50_ms": None, "ttft_p95_ms": None,
                            "success_rate": 1.0, "samples": 154,
                        },
                    }
                ],
            }
        ],
    }
    snap = _snapshot_from_dict(wire, etag='"abc"')
    m = snap.providers[0].models[0]
    assert m.aliases == ("grok-3",)
    assert m.family == "grok-3"
    assert m.variant == "web"


def test_sdk_handles_v20_proxy_without_identity_fields():
    """Older proxy (LMRHv2.0) doesn't emit aliases/family/variant.
    SDK applies defaults — no exception, empty/None defaults."""
    from sdk.python.lmrh_client import _snapshot_from_dict
    wire = {
        "version": "2.0",
        "as_of": "2026-05-09T20:00:00+00:00",
        "window_sec": 3600,
        "providers": [
            {
                "id": "p1", "name": "Old", "type": "openai",
                "priority": 5, "cost_class": "per_call",
                "circuit": "closed", "regions": [],
                "models": [
                    {
                        "model_id": "gpt-4o", "kind": "chat",
                        "context_length": 128000,
                        "native_tools": True, "native_reasoning": False,
                        "metrics": {
                            "cost_per_1m_input_usd": 5.0,
                            "cost_per_1m_output_usd": 15.0,
                            "rated_quota_per_1m_input_usd": None,
                            "latency_p50_ms": 800.0, "latency_p95_ms": 2000.0,
                            "ttft_p50_ms": None, "ttft_p95_ms": None,
                            "success_rate": 0.99, "samples": 500,
                        },
                    }
                ],
            }
        ],
    }
    snap = _snapshot_from_dict(wire, etag='"old"')
    m = snap.providers[0].models[0]
    assert m.aliases == ()
    assert m.family is None
    assert m.variant is None


# ── /v1/models entry shape ────────────────────────────────────────────


def test_v1_models_de_dupes_across_aliases():
    """The de-dupe walk in /v1/models marks all spellings (canonical +
    aliases) seen so a second capability row representing the same
    upstream model is skipped. Pre-v3.4.1 this only de-duped on
    bare model_id, so ``grok-3`` and ``x-ai/grok-3`` both leaked."""
    from app.routing.canonical import collect_canonical_aliases
    seen: set[str] = set()
    # First capability: canonical x-ai/grok-3 with grok-3 as alias
    names_a = collect_canonical_aliases("x-ai/grok-3", ["grok-3"])
    for n in names_a:
        seen.add(n.lower())
    # Second capability: same upstream model, different provider, only
    # registered the bare name. Should be detected as duplicate.
    names_b = collect_canonical_aliases("grok-3", [])
    is_duplicate = any(n.lower() in seen for n in names_b)
    assert is_duplicate, (
        "Bare grok-3 should de-dupe against a row that listed it as alias"
    )
