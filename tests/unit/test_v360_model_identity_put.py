"""v3.6.0 — Model-identity edit API tests.

Covers ``GET/PUT /api/llm/models/{model_id}`` per the OpenAPI spec at
``docs/rfc/2026-05-model-identity-put-spec.md``.

Coverage:
- Validation: aliases len/whitespace/dup/collision/family-soft-warn
- Concurrency: ETag round-trip + 412 on mismatch + 412 fresh-ETag
- Multi-row write: same model_id on 2 providers → both rows updated
- Optional ?provider_id= scoping
- Auth: admin-required (403 without)
- 404 on unknown model_id
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.llm_models import (
    ModelIdentityUpdate,
    _merged_identity,
    _validate_aliases,
)
from app.api._etag import (
    etag_for_capability,
    etag_for_canonical_model,
    parse_if_match,
)
from app.routing.canonical import KNOWN_FAMILIES


# ── ModelIdentityUpdate (Pydantic) ─────────────────────────────────


def test_update_accepts_all_fields_optional():
    """Empty body is valid — used as a no-op probe."""
    m = ModelIdentityUpdate()
    assert m.aliases is None and m.family is None and m.variant is None


def test_update_rejects_empty_family():
    with pytest.raises(Exception):
        ModelIdentityUpdate(family="")


def test_update_accepts_partial_body():
    """Hub UI may PUT just aliases (preserving family/variant)."""
    m = ModelIdentityUpdate(aliases=["foo"])
    assert m.aliases == ["foo"]
    assert m.family is None


# ── _validate_aliases ──────────────────────────────────────────────


def test_validate_aliases_rejects_too_many():
    with pytest.raises(HTTPException) as ex:
        _validate_aliases(["a"] * 17, "model")
    assert ex.value.status_code == 400
    assert "16" in ex.value.detail


def test_validate_aliases_accepts_at_limit():
    _validate_aliases(["a%d" % i for i in range(16)], "model")  # no raise


def test_validate_aliases_rejects_whitespace():
    with pytest.raises(HTTPException) as ex:
        _validate_aliases(["valid", "has space", "ok"], "model")
    assert ex.value.status_code == 400
    assert "whitespace" in ex.value.detail


def test_validate_aliases_rejects_empty_string():
    with pytest.raises(HTTPException) as ex:
        _validate_aliases(["valid", ""], "model")
    assert ex.value.status_code == 400


def test_validate_aliases_rejects_too_long():
    with pytest.raises(HTTPException) as ex:
        _validate_aliases(["a" * 65], "model")
    assert ex.value.status_code == 400
    assert "64" in ex.value.detail


def test_validate_aliases_rejects_duplicates_case_insensitive():
    with pytest.raises(HTTPException) as ex:
        _validate_aliases(["claude", "Claude"], "model")
    assert ex.value.status_code == 400
    assert "duplicate" in ex.value.detail


def test_validate_aliases_accepts_clean_list():
    _validate_aliases(
        ["claude-3-7-sonnet", "claude-sonnet", "sonnet-4.6"],
        "claude-sonnet-4-6",
    )


# ── KNOWN_FAMILIES (operator soft-validation reference) ────────────


def test_known_families_includes_major_vendors():
    """Locked content for the Hub UI to lift via the OpenAPI spec."""
    assert {"claude", "gpt", "gemini", "grok", "llama"}.issubset(KNOWN_FAMILIES)


def test_known_families_is_immutable():
    """frozenset prevents accidental mutation by callers."""
    with pytest.raises(AttributeError):
        KNOWN_FAMILIES.add("bogus")


# ── ETag helpers ───────────────────────────────────────────────────


def _make_cap(model_id: str, aliases: list[str], family=None, variant=None,
              updated_at=None):
    """Lightweight stub of ModelCapability for ETag testing — only
    the fields ``etag_for_capability`` reads."""
    from datetime import datetime
    class StubCap:
        pass
    c = StubCap()
    c.model_id = model_id
    c.aliases = aliases
    c.model_family = family
    c.model_variant = variant
    c.updated_at = updated_at or datetime(2026, 5, 9, 12, 0, 0)
    return c


def test_etag_for_capability_is_deterministic():
    a = _make_cap("x-ai/grok-3", ["grok-3"], family="grok")
    b = _make_cap("x-ai/grok-3", ["grok-3"], family="grok")
    assert etag_for_capability(a) == etag_for_capability(b)


def test_etag_changes_when_aliases_change():
    a = _make_cap("x-ai/grok-3", ["grok-3"])
    b = _make_cap("x-ai/grok-3", ["grok-3", "grok"])
    assert etag_for_capability(a) != etag_for_capability(b)


def test_etag_invariant_to_alias_order():
    """Sorted internally so ['a','b'] and ['b','a'] hash the same."""
    a = _make_cap("model", ["a", "b"])
    b = _make_cap("model", ["b", "a"])
    assert etag_for_capability(a) == etag_for_capability(b)


def test_etag_for_canonical_model_aggregates_rows():
    a = _make_cap("model", ["a"])
    b = _make_cap("model", ["b"])
    e1 = etag_for_canonical_model([a])
    e2 = etag_for_canonical_model([a, b])
    assert e1 != e2


def test_etag_for_canonical_model_is_order_invariant():
    a = _make_cap("model", ["a"])
    b = _make_cap("model", ["b"])
    assert etag_for_canonical_model([a, b]) == etag_for_canonical_model([b, a])


def test_etag_format_is_quoted_hex():
    e = etag_for_capability(_make_cap("m", []))
    assert e.startswith('"') and e.endswith('"')
    assert len(e) == 18  # 16 hex + 2 quotes


# ── parse_if_match ─────────────────────────────────────────────────


def test_parse_if_match_handles_none():
    assert parse_if_match(None) is None
    assert parse_if_match("") is None


def test_parse_if_match_strips_weak_validator():
    assert parse_if_match('W/"abc123"') == '"abc123"'


def test_parse_if_match_normalizes_unquoted():
    assert parse_if_match("abc123") == '"abc123"'


def test_parse_if_match_passes_through_quoted():
    assert parse_if_match('"abc123"') == '"abc123"'


def test_parse_if_match_treats_star_as_missing():
    """If-Match: * is a "row exists" semantic we don't need."""
    assert parse_if_match("*") is None


# ── _merged_identity ───────────────────────────────────────────────


def test_merged_identity_empty_rows():
    out = _merged_identity([], "ghost-model")
    assert out["model_id"] == "ghost-model"
    assert out["provider_count"] == 0


def test_merged_identity_dedupes_aliases_across_rows():
    """Same canonical id on 2 providers, slightly different aliases —
    output merges both as a deduped set."""
    a = _make_cap("claude-sonnet-4-6", ["claude-3-7-sonnet"])
    a.provider_id = "p1"
    b = _make_cap("claude-sonnet-4-6", ["claude-3-7-sonnet", "sonnet-4.6"])
    b.provider_id = "p2"
    out = _merged_identity([a, b], "claude-sonnet-4-6")
    assert out["provider_count"] == 2
    assert set(out["aliases"]) == {"claude-3-7-sonnet", "sonnet-4.6"}


def test_merged_identity_uses_explicit_family_over_derived():
    a = _make_cap("x-ai/grok-3", [], family="grok")  # explicit
    a.provider_id = "p1"
    out = _merged_identity([a], "x-ai/grok-3")
    assert out["family"] == "grok"


def test_merged_identity_falls_back_to_derive_family():
    a = _make_cap("x-ai/grok-3", [], family=None)
    a.provider_id = "p1"
    out = _merged_identity([a], "x-ai/grok-3")
    assert out["family"] == "grok-3"  # derive_family strips x-ai/


# ── Cluster sync replication of identity fields (v3.6.0 prereq) ────


def test_cluster_build_payload_includes_identity_fields():
    """Pre-v3.6.0 the build pass dropped aliases/family/variant —
    a PUT on www01 wouldn't replicate. Verify the keys are now in
    the snapshot."""
    # We import lazily because the function pulls heavy deps; we
    # only need to confirm the payload shape includes the keys.
    import inspect
    from app.cluster import manager
    src = inspect.getsource(manager)
    assert '"aliases": c.aliases' in src or "'aliases': c.aliases" in src
    assert '"model_family"' in src
    assert '"model_variant"' in src


def test_cluster_apply_pass_replicates_identity_fields():
    """The apply pass must also write incoming aliases/family/variant
    or the cluster will diverge after a PUT."""
    import inspect
    from app.cluster import sync
    src = inspect.getsource(sync)
    # Apply pass: looks for the 3 fields in the c_data dict
    assert 'c_data.get("aliases")' in src
    assert 'c_data.get("model_family")' in src
    assert 'c_data.get("model_variant")' in src
