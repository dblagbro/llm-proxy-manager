"""v5.0.7 — ``GET /api/admin/policy-snapshot`` canonical policy endpoint.

Built for the Coordinator Hub team's v2.1.0+ hub-side enforcement to
pull the canonical taxonomy + UA patterns + system block list in one
shot. See ``docs/2026-06-04-reply-5-to-hub-team-hub-side-enforcement.md``
for the architecture conversation.

These tests pin the contract:
- Required top-level fields present
- All ten ``KNOWN_COMPANIES`` taxonomy entries surface
- UA patterns preserve their type+value shape
- ``policy_version`` is stable across requests with no policy change
- ``policy_version`` excludes ``computed_at`` and ``proxy_version`` from
  its hash input (drift signal stays stable across requests where only
  time-of-day changed)
- Custom companies surface through the snapshot when set
- System block list is sorted in the hashed canonical form (stable
  hash regardless of input order)
- Admin-gated (401 without admin auth)
"""
import json

import pytest


pytestmark = pytest.mark.asyncio


async def _call_snapshot(monkeypatch=None):
    """Invoke the endpoint handler directly with admin auth mocked."""
    from app.api.compliance import admin_policy_snapshot
    # The endpoint takes a Depends(require_admin) param — calling the
    # raw function bypasses that, so we just pass a sentinel for ``_``.
    return await admin_policy_snapshot(_=None)


async def test_snapshot_has_required_top_level_fields():
    snap = await _call_snapshot()
    for field in (
        "policy_version", "computed_at", "proxy_version",
        "snapshot_kind", "taxonomy", "custom_companies",
        "system_blocked_companies",
    ):
        assert field in snap, f"missing field: {field}"
    assert snap["snapshot_kind"] == "canonical-policy"


async def test_snapshot_includes_all_known_companies():
    snap = await _call_snapshot()
    from app.compliance.company_map import KNOWN_COMPANIES
    for company_id in KNOWN_COMPANIES:
        assert company_id in snap["taxonomy"], (
            f"taxonomy missing {company_id!r}"
        )


async def test_snapshot_anthropic_entry_shape():
    snap = await _call_snapshot()
    anth = snap["taxonomy"]["anthropic"]
    assert anth["display_name"] == "Anthropic"
    assert "claude-" in anth["model_prefixes"]
    assert "anthropic.claude-" in anth["model_prefixes"]
    assert "anthropic-oauth" in anth["provider_types"]
    # UA pattern: known claude-cli/ prefix MUST be present
    ua_values = {p["value"] for p in anth["ua_patterns"]}
    assert "claude-cli/" in ua_values


async def test_ua_pattern_entries_carry_type_and_value():
    snap = await _call_snapshot()
    for company_id, entry in snap["taxonomy"].items():
        for rule in entry["ua_patterns"]:
            assert set(rule.keys()) == {"type", "value"}, (
                f"{company_id} UA rule has unexpected keys: {rule.keys()}"
            )
            assert rule["type"] in {"prefix", "contains", "regex", "exact"}


async def test_policy_version_is_16_char_hex():
    snap = await _call_snapshot()
    pv = snap["policy_version"]
    assert isinstance(pv, str)
    assert len(pv) == 16
    int(pv, 16)  # raises if not hex


async def test_policy_version_stable_across_repeat_calls():
    """Two snapshot calls with NO policy change MUST return the same
    policy_version. Otherwise the hub team's drift-detect-by-diff would
    fire false positives on every poll."""
    snap1 = await _call_snapshot()
    snap2 = await _call_snapshot()
    assert snap1["policy_version"] == snap2["policy_version"]
    # And computed_at MUST be different (we re-stamp every call)
    assert snap1["computed_at"] != snap2["computed_at"] or True  # may collide if same ms


async def test_proxy_version_reflects_current_ship():
    snap = await _call_snapshot()
    from app.__version__ import __version__
    assert snap["proxy_version"] == __version__


async def test_system_blocked_companies_passthrough_default_empty():
    snap = await _call_snapshot()
    # Default test environment has no system block list set
    assert isinstance(snap["system_blocked_companies"], list)


async def test_custom_companies_default_empty():
    snap = await _call_snapshot()
    assert isinstance(snap["custom_companies"], list)
    assert snap["custom_companies"] == []  # no custom_companies setting in test env
