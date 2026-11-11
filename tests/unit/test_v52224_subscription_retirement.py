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
    validate_provider_type_change,
)
from app.api.providers import ProviderCreate, ProviderUpdate


class TestTheSets:
    def test_dead_types_are_retired(self):
        assert RETIRED_PROVIDER_TYPES == {"ChatGPT-oauth-plan"}

    def test_grok_web_is_deprecated_not_retired(self):
        """Moved out of RETIRED on 2026-09-26: the operator confirmed the
        bridge is still needed. Claiming a type is retired while a live
        provider depends on it is the doc/behaviour drift this repo keeps
        getting bitten by."""
        assert "grok-web" in DEPRECATED_PROVIDER_TYPES
        assert "grok-web" not in RETIRED_PROVIDER_TYPES

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
    @pytest.mark.parametrize("ptype", ["grok-web", "claude-oauth"])
    def test_create_is_refused_at_construction(self, ptype):
        """CREATE is absolute: a new deprecated/retired provider is closed."""
        with pytest.raises(Exception):
            ProviderCreate(name="x", provider_type=ptype)

    @pytest.mark.parametrize("ptype", ["grok-web", "claude-oauth"])
    def test_update_constructs_for_a_deprecated_type(self, ptype):
        """UPDATE is row-relative (v5.22.44). A PUT re-submits the row's own
        current provider_type; constructing ProviderUpdate must NOT reject it,
        or every edit/re-auth of a live deprecated provider 422s. The real
        gate (refuse a CHANGE into a closed type) runs in update_provider()
        where the existing type is known — see TestUpdateChangeGate."""
        assert ProviderUpdate(name="x", provider_type=ptype).provider_type == ptype

    @pytest.mark.parametrize("model", [ProviderCreate, ProviderUpdate])
    def test_permitted_type_still_constructs(self, model):
        assert model(name="x", provider_type="openai").provider_type == "openai"

    @pytest.mark.parametrize("model", [ProviderCreate, ProviderUpdate])
    def test_the_ssrf_guard_survived_the_mixin_refactor(self, model):
        """Both validators moved into one mixin; neither may be lost.
        Neutralizing the provider_type validator on ProviderUpdate must not
        take the base_url SSRF guard with it."""
        with pytest.raises(Exception):
            model(name="x", provider_type="openai",
                  base_url="http://169.254.169.254/latest/meta-data/")


class TestUpdateChangeGate:
    """v5.22.44 — the row-relative gate for PUT /api/providers/{id}.

    Regression: re-authing the live ``Devin-Anthropic-Max-VG`` (claude-oauth)
    returned 422 because the create-time gate fired on the row's own
    unchanged deprecated type, contradicting the DEPRECATED tier's contract
    that the existing row keeps working (incl. edits + OAuth re-auth)."""

    @pytest.mark.parametrize("ptype", ["claude-oauth", "grok-web", "ChatGPT-oauth-plan"])
    def test_keeping_a_closed_type_is_allowed(self, ptype):
        # old == new: the operator is editing/re-authing an existing row.
        assert validate_provider_type_change(ptype, ptype) is None

    @pytest.mark.parametrize("closed", ["claude-oauth", "grok-web", "ChatGPT-oauth-plan"])
    def test_changing_into_a_closed_type_is_refused(self, closed):
        with pytest.raises(ValueError):
            validate_provider_type_change("openai", closed)

    def test_changing_between_open_types_is_allowed(self):
        assert validate_provider_type_change("openai", "anthropic") is None

    def test_none_new_passes(self):
        assert validate_provider_type_change("claude-oauth", None) is None


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
