"""v5.22.21 — cross-family fallback must not pick a provider it cannot dispatch.

Four of seven enabled providers have their OWN dispatcher and never go through
litellm — their entry in ``PROVIDER_TYPE_TO_LITELLM`` exists only to populate
the ``X-Resolved-Model`` header:

    claude-oauth        -> messages.py, direct httpx to platform.claude.com
    ChatGPT-oauth-plan  -> _codex_oauth_dispatch, direct httpx to chatgpt.com
    grok-web            -> app.providers.grok_web, replays a browser session

Their credentials are OAuth session tokens, not API keys. The cross-family
fallback path rewrites the model and dispatches through litellm, and used to
leave ``available`` unfiltered — so it could hand a session token to a vendor
API endpoint. Measured 2026-09-18 against Devin-Codex-Gmail:

    litellm.acompletion('openai/gpt-5.5', <chatgpt oauth token>)
      -> OpenAIException 401 "insufficient permissions ...
         Missing scopes: model.request"

Which is why reauthing that provider changed nothing — the credential was
never the problem.

This was NOT Codex-specific. Both Anthropic providers (priorities 7 and 8) are
the same shape, so the tests below cover every affected type rather than the
one that happened to get picked.
"""

import pytest

from app.routing.litellm_binding import (
    OWN_DISPATCHER_PROVIDER_TYPES,
    PROVIDER_TYPE_TO_LITELLM,
)
from app.routing.router import _drop_own_dispatcher_types


class _P:
    def __init__(self, name, provider_type):
        self.name = name
        self.provider_type = provider_type

    def __repr__(self):
        return f"<{self.name}:{self.provider_type}>"


LITELLM_OK = [_P("OpenRouter", "openrouter"), _P("OpenAI", "openai"), _P("Cohere", "cohere")]


class TestTheSetItself:
    @pytest.mark.parametrize("ptype", ["claude-oauth", "ChatGPT-oauth-plan", "grok-web"])
    def test_own_dispatcher_types_are_listed(self, ptype):
        assert ptype in OWN_DISPATCHER_PROVIDER_TYPES

    def test_cursor_oauth_is_deliberately_excluded(self):
        """cursor-oauth dispatches through the Cursor-To-OpenAI sidecar, which
        really does speak the OpenAI wire format — litellm is correct for it.
        Adding it here would break a working provider."""
        assert "cursor-oauth" not in OWN_DISPATCHER_PROVIDER_TYPES

    @pytest.mark.parametrize("ptype", ["openai", "openrouter", "cohere", "anthropic", "azure"])
    def test_ordinary_api_key_types_are_not_listed(self, ptype):
        assert ptype not in OWN_DISPATCHER_PROVIDER_TYPES

    def test_every_listed_type_is_a_real_provider_type(self):
        """Guards against a typo silently excluding nothing."""
        for ptype in OWN_DISPATCHER_PROVIDER_TYPES:
            assert ptype in PROVIDER_TYPE_TO_LITELLM, f"{ptype} is not a known provider_type"


class TestFiltering:
    @pytest.mark.parametrize(
        "ptype,name",
        [
            ("ChatGPT-oauth-plan", "Devin-Codex-Gmail"),
            ("claude-oauth", "Devin-Anthropic-Max-VG"),
            ("claude-oauth", "Devin-Anthropic-Max-Gmail"),
            ("grok-web", "Grok-Web-Devin"),
        ],
    )
    def test_each_affected_provider_is_dropped(self, ptype, name):
        """Named after the real fleet: this is the whole exposure, not just
        the provider that happened to get picked."""
        out = _drop_own_dispatcher_types([_P(name, ptype)] + LITELLM_OK)
        assert all(p.provider_type != ptype or p.name != name for p in out)
        assert len(out) == len(LITELLM_OK)

    def test_litellm_capable_providers_survive(self):
        out = _drop_own_dispatcher_types(list(LITELLM_OK))
        assert out == LITELLM_OK

    def test_cursor_oauth_survives(self):
        cursor = _P("Cursor", "cursor-oauth")
        assert cursor in _drop_own_dispatcher_types([cursor] + LITELLM_OK)

    def test_empty_input(self):
        assert _drop_own_dispatcher_types([]) == []


class TestDegradedRatherThanBroken:
    def test_all_candidates_excluded_returns_them_unchanged(self):
        """If filtering would empty the list, keep it. An empty candidate list
        becomes a 503, and a 503 is a worse answer than letting the circuit
        breaker handle a provider that might still work — the caller is
        already on a degraded, substituting path."""
        only_bypass = [
            _P("Codex", "ChatGPT-oauth-plan"),
            _P("Anthropic-VG", "claude-oauth"),
        ]
        assert _drop_own_dispatcher_types(only_bypass) == only_bypass


class TestWiredIntoBothFallbackSites:
    """The helper is useless unless both cross-family branches call it."""

    def _src(self):
        from pathlib import Path

        return Path("app/routing/router.py").read_text()

    def test_called_at_both_sites(self):
        assert self._src().count("_drop_own_dispatcher_types(available)") == 2

    def test_same_family_routing_is_untouched(self):
        """The filter must sit in the cross-family branches only. An Anthropic
        request matches claude-oauth in family_types and must still route to
        it normally."""
        src = self._src()
        needle = "_drop_own_dispatcher_types(available)"
        sites = [i for i in range(len(src)) if src.startswith(needle, i)]
        assert sites, "helper is never called"
        for call_at in sites:
            # Look both ways: at the family-intersection site the flag is set
            # on the line AFTER the call, at the capability site it is before.
            window = src[max(0, call_at - 1200):call_at + 400]
            assert "cross_family" in window, (
                "filter applied outside a cross-family branch — this would "
                "break normal same-family routing to OAuth providers"
            )
