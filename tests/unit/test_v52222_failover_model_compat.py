"""v5.22.22 — a failover must not force a model onto a provider that can't serve it.

The grok-web failover in messages.py / completions.py cleared
``cross_family_fallback`` and rebuilt ``litellm_model`` from the caller's
original model, to "preserve the caller's model intent" (v5.0.23). That is
correct for the case it was written for — OpenRouter, where
``build_litellm_model`` maps ``grok-3`` to ``openrouter/x-ai/grok-3``.

It is wrong for every other target. Measured over 24h on 2026-09-22:
**41 `grok_web.failover_to` events produced 41 failures**, every one shaped

    [name=Devin-Anthropic-Max-VG type=claude-oauth
     litellm_model=anthropic/grok-3 requested=grok-3 cross_family=False]

``grok-3``'s family is ``{openrouter, grok-web, grok}``; ``claude-oauth`` is
not in it. The failover forced the pairing anyway.

Two things made this hard to see. The flag it stamps — ``cross_family=False``
— is exactly the flag the v5.22.21 guard keys on, so that fix could not catch
it. And the provider it lands on has its **own dispatcher**, so the request
also went to litellm when it should never have.
"""

import pytest

from app.routing.litellm_binding import OWN_DISPATCHER_PROVIDER_TYPES
from app.routing.router import failover_preserves_model


class _P:
    def __init__(self, provider_type, name="p"):
        self.provider_type = provider_type
        self.name = name


class TestTheCaseItWasWrittenFor:
    def test_openrouter_still_preserves_grok3(self):
        """OpenRouter genuinely serves grok-3 via openrouter/x-ai/grok-3.
        Breaking this would regress v5.0.23."""
        assert failover_preserves_model(_P("openrouter"), "grok-3") is True

    def test_same_family_targets_preserve_the_model(self):
        assert failover_preserves_model(_P("openai"), "gpt-4o") is True
        assert failover_preserves_model(_P("anthropic"), "claude-sonnet-4-6") is True


class TestTheLiveBug:
    @pytest.mark.parametrize("model", ["grok-3", "grok-4"])
    def test_claude_oauth_cannot_serve_a_grok_model(self, model):
        """The exact production pairing: anthropic/grok-3."""
        assert failover_preserves_model(_P("claude-oauth"), model) is False

    def test_openai_cannot_serve_a_grok_model(self):
        assert failover_preserves_model(_P("openai"), "grok-3") is False

    def test_cohere_cannot_serve_a_grok_model(self):
        assert failover_preserves_model(_P("cohere"), "grok-3") is False


class TestEdges:
    def test_unknown_family_does_not_constrain(self):
        """_model_family_provider_types returns None for unknown families and
        the codebase treats that as 'don't constrain'. Keep that semantic —
        tightening it here would break new models on the day they ship."""
        assert failover_preserves_model(_P("openai"), "some-model-shipped-today") is True

    def test_no_model_means_no_preservation(self):
        assert failover_preserves_model(_P("openai"), None) is False
        assert failover_preserves_model(_P("openai"), "") is False


class TestWiring:
    def _src(self, fn):
        # v5.22.37 — read the endpoint's whole surface. Its failover code moved
        # into the dispatch modules when both handlers were split for their LOC
        # pins, so naming a single file here goes stale on the next move.
        from tests.unit._handler_surface import handler_source
        return handler_source(fn)

    @pytest.mark.parametrize("fn", ["app/api/messages.py", "app/api/completions.py"])
    def test_both_failover_paths_gate_on_it(self, fn):
        src = self._src(fn)
        assert "failover_preserves_model(" in src, f"{fn} does not gate the failover"
        assert "grok_web.failover_model_substituted" in src, (
            f"{fn} substitutes silently — an operator cannot see it happen"
        )

    @pytest.mark.parametrize("fn", ["app/api/messages.py", "app/api/completions.py"])
    def test_failover_excludes_own_dispatcher_types(self, fn):
        """The failover falls through to the litellm dispatch path, so a
        provider with its own dispatcher must not be selectable there."""
        src = self._src(fn)
        assert "excluded_provider_types=OWN_DISPATCHER_PROVIDER_TYPES" in src

    @pytest.mark.parametrize("fn", ["app/api/messages.py", "app/api/completions.py"])
    def test_substitution_sets_the_cross_family_flag(self, fn):
        """Clearing the flag on a substituted route is what hid this from the
        v5.22.21 guard and from the disclosure headers."""
        src = self._src(fn)
        after = src.split("failover_preserves_model(", 1)[1]
        assert "cross_family_fallback = True" in after[:1200]

    def test_claude_oauth_is_an_own_dispatcher_type(self):
        """Ties the two halves together: the provider in the production
        failure is exactly one the failover should never have selected."""
        assert "claude-oauth" in OWN_DISPATCHER_PROVIDER_TYPES
