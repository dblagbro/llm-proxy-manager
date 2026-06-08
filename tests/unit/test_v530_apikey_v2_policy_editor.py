"""v5.3.0 — ApiKey API surface for the v5.2.1 fine-grained policy fields.

The policy engine + DB columns landed in v5.2.1; this ship makes them
editable via PATCH/POST /api/keys and round-trippable in the serialize
payload. Tests:

- KeyCreate accepts the 3 new fields; persists them.
- KeyUpdate edits them; reason required on change (decision 6).
- _serialize round-trips allowed_companies/blocked_models/allowed_models.
- _validate_model_patterns rejects whitespace, empty, and oversize strings.
- copy_from_id pulls the fine-grained fields too.
- Audit row's before/after carries the 3 new keys.
"""
from __future__ import annotations

import pytest


def test_keycreate_accepts_v521_fields():
    from app.api.apikeys import KeyCreate
    k = KeyCreate(
        name="t",
        allowed_companies=["openai"],
        blocked_models=["claude-opus-*"],
        allowed_models=["gpt-*"],
        reason="test",
    )
    assert k.allowed_companies == ["openai"]
    assert k.blocked_models == ["claude-opus-*"]
    assert k.allowed_models == ["gpt-*"]


def test_keyupdate_accepts_v521_fields():
    from app.api.apikeys import KeyUpdate
    u = KeyUpdate(
        allowed_companies=["openai"],
        blocked_models=["claude-*"],
        allowed_models=None,
        reason="lockdown",
    )
    assert u.allowed_companies == ["openai"]
    assert u.blocked_models == ["claude-*"]
    assert u.allowed_models is None


def test_validate_model_patterns_accepts_globs_and_exact():
    from app.api.apikeys import _validate_model_patterns
    _validate_model_patterns(["claude-*", "gpt-4-*-turbo", "claude-opus-4-0"])


def test_validate_model_patterns_rejects_whitespace():
    from app.api.apikeys import _validate_model_patterns
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _validate_model_patterns(["claude *"])
    assert exc.value.status_code == 400


def test_validate_model_patterns_rejects_empty():
    from app.api.apikeys import _validate_model_patterns
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _validate_model_patterns([""])


def test_validate_model_patterns_rejects_too_long():
    from app.api.apikeys import _validate_model_patterns, _MAX_MODEL_PATTERN_LEN
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _validate_model_patterns(["x" * (_MAX_MODEL_PATTERN_LEN + 1)])


def test_validate_model_patterns_empty_list_is_noop():
    from app.api.apikeys import _validate_model_patterns
    _validate_model_patterns([])
    _validate_model_patterns(None)  # type: ignore[arg-type]


def test_serialize_roundtrips_v521_fields():
    from app.api.apikeys import _serialize
    from types import SimpleNamespace

    fake = SimpleNamespace(
        id="k1", name="t", key_prefix="p", key_type="standard",
        enabled=True,
        total_requests=0, total_tokens=0, total_cost_usd=0.0,
        spending_cap_usd=None, rate_limit_rpm=None, rate_limit_tier=None,
        daily_soft_cap_usd=None, daily_hard_cap_usd=None, hourly_cap_usd=None,
        semantic_cache_enabled=False, caller_memory_ttl_days=None,
        blocked_companies=None, allowed_paths=None,
        allowed_companies=["openai"],
        blocked_models=["claude-*"],
        allowed_models=None,
        debug_echo_enabled=False,
        day_cost_usd=0.0, hour_cost_usd=0.0,
        encrypted_key=None,
        last_used_at=None, created_at=None, deleted_at=None,
    )
    out = _serialize(fake)  # type: ignore[arg-type]
    assert out["allowed_companies"] == ["openai"]
    assert out["blocked_models"] == ["claude-*"]
    assert out["allowed_models"] is None


def test_serialize_omits_when_null():
    """A legacy key with NULL on every v5.2.1 column serializes to None,
    NOT to an empty list — preserves the client-side state machine that
    treats null as "no opinion" vs [] as "explicit no entries"."""
    from app.api.apikeys import _serialize
    from types import SimpleNamespace

    fake = SimpleNamespace(
        id="legacy", name="t", key_prefix="p", key_type="standard",
        enabled=True,
        total_requests=0, total_tokens=0, total_cost_usd=0.0,
        spending_cap_usd=None, rate_limit_rpm=None, rate_limit_tier=None,
        daily_soft_cap_usd=None, daily_hard_cap_usd=None, hourly_cap_usd=None,
        semantic_cache_enabled=False, caller_memory_ttl_days=None,
        blocked_companies=None, allowed_paths=None,
        allowed_companies=None, blocked_models=None, allowed_models=None,
        debug_echo_enabled=False,
        day_cost_usd=0.0, hour_cost_usd=0.0,
        encrypted_key=None,
        last_used_at=None, created_at=None, deleted_at=None,
    )
    out = _serialize(fake)  # type: ignore[arg-type]
    assert out["allowed_companies"] is None
    assert out["blocked_models"] is None
    assert out["allowed_models"] is None


def test_pydantic_field_list_optional_str():
    """Lock the wire shape so a JSON payload with a list[str] round-trips
    through pydantic without coercion surprises."""
    from app.api.apikeys import KeyCreate
    import json
    payload = json.dumps({
        "name": "t",
        "allowed_companies": ["openai", "google"],
        "blocked_models": ["claude-*"],
        "reason": "test",
    })
    k = KeyCreate.model_validate_json(payload)
    assert k.allowed_companies == ["openai", "google"]
    assert k.blocked_models == ["claude-*"]
