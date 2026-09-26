"""v5.22.24 — subscription-OAuth provider types are closed to new providers.

Acting on the NARROW recommendation in `docs/market-review.md` (2026-09-26).
The driving evidence is Anthropic's Claude Code legal and compliance page:
subscription OAuth is "intended exclusively" for Claude Code and native
Anthropic applications, Anthropic "does not permit third-party developers ...
to route requests through Free, Pro, or Max plan credentials on behalf of
their users", and developers "may not collect, store, or intermediate
Claude.ai credentials or session tokens".

Our telemetry agreed independently: `Devin-Codex-Gmail` returned 1 success in
4,457 requests over 24 days; `Grok-Web-Devin` was 0/27 in 12 hours.

The retirement is deliberately **staged**, and that is the property most worth
pinning. `claude-oauth` is DEPRECATED, not RETIRED, because at the time of the
change `Devin-Anthropic-Max-VG` was the healthiest provider in the fleet
(16 successes, 0 failures over 12h) while two of three metered providers were
unverified. Closing the write path costs nothing; pulling a serving provider
would have traded a compliance risk for an outage.
"""

import pytest

from app.api._provider_type_policy import (
    DEPRECATED_PROVIDER_TYPES,
    RETIRED_PROVIDER_TYPES,
    validate_provider_type,
)
from app.api.providers import ProviderCreate, ProviderUpdate


class TestTheSets:
    def test_dead_types_are_retired(self):
        assert RETIRED_PROVIDER_TYPES == {"ChatGPT-oauth-plan", "grok-web"}

    def test_claude_oauth_is_deprecated_not_retired(self):
        """Staging matters: a serving provider must not be pulled by a code
        change. Promoting this to RETIRED is the Phase 2 gate."""
        assert "claude-oauth" in DEPRECATED_PROVIDER_TYPES
        assert "claude-oauth" not in RETIRED_PROVIDER_TYPES

    def test_the_two_sets_do_not_overlap(self):
        assert not (RETIRED_PROVIDER_TYPES & DEPRECATED_PROVIDER_TYPES)

    def test_cursor_oauth_is_not_covered(self):
        """Cursor dispatches through a sidecar on its own terms, which this
        review did not assess. Sweeping it in would be unevidenced."""
        assert "cursor-oauth" not in RETIRED_PROVIDER_TYPES
        assert "cursor-oauth" not in DEPRECATED_PROVIDER_TYPES


class TestTheGate:
    @pytest.mark.parametrize("ptype", ["ChatGPT-oauth-plan", "grok-web", "claude-oauth"])
    def test_subscription_types_are_refused(self, ptype):
        with pytest.raises(ValueError):
            validate_provider_type(ptype)

    @pytest.mark.parametrize("ptype", ["openai", "cohere", "openrouter", "anthropic", "azure"])
    def test_api_key_types_are_accepted(self, ptype):
        assert validate_provider_type(ptype) == ptype

    def test_empty_passes_through(self):
        assert validate_provider_type(None) is None
        assert validate_provider_type("") == ""

    def test_message_points_at_the_evidence(self):
        """An operator hitting this needs to know why, not just that."""
        with pytest.raises(ValueError, match="market-review"):
            validate_provider_type("grok-web")


class TestWiredIntoBothModels:
    @pytest.mark.parametrize("model", [ProviderCreate, ProviderUpdate])
    @pytest.mark.parametrize("ptype", ["grok-web", "claude-oauth"])
    def test_refused_on_create_and_update(self, model, ptype):
        with pytest.raises(Exception):
            model(name="x", provider_type=ptype)

    @pytest.mark.parametrize("model", [ProviderCreate, ProviderUpdate])
    def test_permitted_type_still_constructs(self, model):
        assert model(name="x", provider_type="openai").provider_type == "openai"

    @pytest.mark.parametrize("model", [ProviderCreate, ProviderUpdate])
    def test_the_ssrf_guard_survived_the_mixin_refactor(self, model):
        """Both validators moved into one mixin; neither may be lost."""
        with pytest.raises(Exception):
            model(name="x", provider_type="openai",
                  base_url="http://169.254.169.254/latest/meta-data/")


class TestGuardsWritesOnly:
    def test_policy_module_does_not_touch_dispatch(self):
        """This is a gate, not a demolition. Existing rows must keep serving,
        so the policy must not reach into routing or dispatch."""
        import ast
        from pathlib import Path

        # AST, not raw text: the module docstring discusses dispatch on
        # purpose, to explain why this is a gate rather than a demolition.
        tree = ast.parse(Path("app/api/_provider_type_policy.py").read_text())
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        for forbidden in ("litellm", "routing", "cluster"):
            assert not any(forbidden in m for m in imported), (
                f"policy module imports {forbidden}: {imported}"
            )
