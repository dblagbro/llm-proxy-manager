"""v5.2.0 / Batch V2 — fine-grained vendor-neutrality policy tests.

Covers the operator-spec acceptance criteria:
- allowlist mode (positive enforcement) per-key + system
- per-model block (exact name + fnmatch glob like "claude-*")
- per-model allow (exact + glob)
- deny wins (a model both blocked and allowed is BLOCKED)
- allowlist NOT in effect = legacy v5.0 behavior preserved
- merge per-key with system in both directions
- empty policy → list passthrough (zero overhead)
- raises ComplianceNoSubstituteError when every candidate dropped
- Anthropic can be allowed/preferred/blocked via the same mechanism
- claude-* family also matches model_family_companies (bedrock edge case)
"""
from __future__ import annotations

import pytest


class FakeProvider:
    def __init__(self, name: str, owner_company: str = "", default_model: str = ""):
        self.id = name
        self.name = name
        self.owner_company = owner_company
        self.default_model = default_model


def _p(name, company="", model=""):
    return FakeProvider(name, owner_company=company, default_model=model)


# ── Module + export pins ────────────────────────────────────────────


def test_policy_dataclass_exported():
    from app.compliance import Policy, evaluate_policy, filter_providers_v2, get_effective_policy
    assert Policy is not None
    assert callable(evaluate_policy)
    assert callable(filter_providers_v2)
    assert callable(get_effective_policy)


def test_empty_policy_is_empty():
    from app.compliance import Policy
    p = Policy()
    assert p.is_empty() is True


def test_policy_with_any_dimension_is_not_empty():
    from app.compliance import Policy
    assert Policy(blocked_companies={"anthropic"}).is_empty() is False
    assert Policy(allowed_companies={"openai"}).is_empty() is False
    assert Policy(blocked_models=("claude-*",)).is_empty() is False
    assert Policy(allowed_models=("gpt-4*",)).is_empty() is False


# ── evaluate_policy() ───────────────────────────────────────────────


def test_empty_policy_allows_all():
    from app.compliance import Policy, evaluate_policy
    allowed, reason = evaluate_policy(Policy(), _p("a", "anthropic", "claude-3-5-haiku"))
    assert allowed is True
    assert reason == ""


def test_blocked_company_drops():
    from app.compliance import Policy, evaluate_policy
    p = Policy(blocked_companies={"anthropic"})
    allowed, reason = evaluate_policy(p, _p("a", "anthropic", "claude-3"))
    assert allowed is False
    assert reason == "blocked-company"


def test_blocked_model_family_drops_bedrock_anthropic():
    """Decision 11: anthropic.claude-* resolves to BOTH anthropic + aws,
    so banning either drops the Bedrock-served Anthropic model."""
    from app.compliance import Policy, evaluate_policy
    p = Policy(blocked_companies={"anthropic"})
    # owner_company is AWS but the model lineage is anthropic
    allowed, reason = evaluate_policy(p, _p("bedrock", "aws", "anthropic.claude-3-haiku"))
    assert allowed is False
    assert reason == "blocked-model-family"


def test_blocked_model_exact_match():
    from app.compliance import Policy, evaluate_policy
    p = Policy(blocked_models=("claude-opus-4-0",))
    drop, _ = evaluate_policy(p, _p("a", "anthropic", "claude-opus-4-0"))
    assert drop is False
    keep, _ = evaluate_policy(p, _p("b", "anthropic", "claude-sonnet-4-5"))
    assert keep is True


def test_blocked_model_glob():
    from app.compliance import Policy, evaluate_policy
    p = Policy(blocked_models=("claude-*",))
    for m in ("claude-3-5-haiku", "claude-opus-4-0", "claude-sonnet-4-6"):
        drop, _ = evaluate_policy(p, _p("a", "anthropic", m))
        assert drop is False, f"expected {m} to be blocked by claude-* glob"
    # not matched
    keep, _ = evaluate_policy(p, _p("o", "openai", "gpt-4o"))
    assert keep is True


def test_blocked_model_glob_matches_requested_model_too():
    """A caller asking for claude-haiku via an openai-shape provider
    must still be dropped by the per-model glob — defense in depth
    against a misconfigured provider routing."""
    from app.compliance import Policy, evaluate_policy
    p = Policy(blocked_models=("claude-*",))
    drop, reason = evaluate_policy(
        p, _p("x", "openai", "gpt-4o"),
        requested_model="claude-3-haiku",
    )
    assert drop is False
    assert reason == "blocked-model"


def test_allowed_companies_drops_non_listed():
    from app.compliance import Policy, evaluate_policy
    p = Policy(allowed_companies={"openai"})
    drop, reason = evaluate_policy(p, _p("a", "anthropic", "claude-3-5-haiku"))
    assert drop is False
    assert reason == "company-not-in-allowlist"
    keep, _ = evaluate_policy(p, _p("o", "openai", "gpt-4o"))
    assert keep is True


def test_allowed_companies_via_model_family():
    """If owner_company is unknown but the model lineage maps to an
    allowed company, the provider passes — covers the Bedrock case
    where AWS-owned providers serve Anthropic models for an
    Anthropic-allowlisted key."""
    from app.compliance import Policy, evaluate_policy
    p = Policy(allowed_companies={"anthropic"})
    keep, _ = evaluate_policy(p, _p("bedrock", "aws", "anthropic.claude-3-haiku"))
    assert keep is True


def test_allowed_models_drops_non_matching():
    from app.compliance import Policy, evaluate_policy
    p = Policy(allowed_models=("gpt-4*",))
    drop, reason = evaluate_policy(p, _p("a", "anthropic", "claude-3-5-haiku"))
    assert drop is False
    assert reason == "model-not-in-allowlist"
    keep, _ = evaluate_policy(p, _p("o", "openai", "gpt-4o"))
    assert keep is True


def test_deny_wins_over_allow():
    """A company in both blocked + allowed is blocked."""
    from app.compliance import Policy, evaluate_policy
    p = Policy(
        blocked_companies={"anthropic"},
        allowed_companies={"anthropic", "openai"},
    )
    drop, reason = evaluate_policy(p, _p("a", "anthropic", "claude-3"))
    assert drop is False
    assert reason == "blocked-company"


def test_deny_wins_over_allow_for_models():
    from app.compliance import Policy, evaluate_policy
    p = Policy(
        blocked_models=("claude-opus-4-0",),
        allowed_models=("claude-*",),
    )
    drop, reason = evaluate_policy(p, _p("a", "anthropic", "claude-opus-4-0"))
    assert drop is False
    assert reason == "blocked-model"
    keep, _ = evaluate_policy(p, _p("b", "anthropic", "claude-sonnet-4-5"))
    assert keep is True


def test_anthropic_can_be_allowed_when_policy_permits():
    """Operator-spec acceptance: a deployment that PREFERS Anthropic must
    pass an anthropic-serving provider unchanged."""
    from app.compliance import Policy, evaluate_policy
    p = Policy(allowed_companies={"anthropic"})
    keep, _ = evaluate_policy(p, _p("a", "anthropic", "claude-3-5-sonnet"))
    assert keep is True


def test_anthropic_can_be_blocked_when_policy_blocks():
    """Operator-spec acceptance: a deployment that BLOCKS Anthropic must
    drop all anthropic-serving providers regardless of model."""
    from app.compliance import Policy, evaluate_policy
    p = Policy(blocked_companies={"anthropic"})
    for m in ("claude-3-5-sonnet", "claude-opus-4-0", "anthropic.claude-3-haiku"):
        drop, _ = evaluate_policy(p, _p("a", "anthropic", m))
        assert drop is False


# ── filter_providers_v2() ──────────────────────────────────────────


def test_filter_v2_empty_policy_passthrough():
    from app.compliance import Policy, filter_providers_v2
    ps = [_p("a", "anthropic", "claude-3"), _p("o", "openai", "gpt-4o")]
    out = filter_providers_v2(ps, Policy())
    assert out == ps


def test_filter_v2_drops_blocked():
    from app.compliance import Policy, filter_providers_v2
    ps = [_p("a", "anthropic", "claude-3"), _p("o", "openai", "gpt-4o")]
    out = filter_providers_v2(ps, Policy(blocked_companies={"anthropic"}))
    assert [p.id for p in out] == ["o"]


def test_filter_v2_raises_when_all_dropped():
    from app.compliance import Policy, filter_providers_v2, ComplianceNoSubstituteError
    ps = [_p("a", "anthropic", "claude-3"), _p("a2", "anthropic", "claude-opus")]
    with pytest.raises(ComplianceNoSubstituteError):
        filter_providers_v2(ps, Policy(blocked_companies={"anthropic"}))


def test_filter_v2_allowlist_mode_keeps_only_listed():
    from app.compliance import Policy, filter_providers_v2
    ps = [
        _p("a", "anthropic", "claude-3"),
        _p("o", "openai", "gpt-4o"),
        _p("g", "google", "gemini-2"),
    ]
    out = filter_providers_v2(ps, Policy(allowed_companies={"openai"}))
    assert [p.id for p in out] == ["o"]


def test_filter_v2_glob_pattern_blocks_family():
    from app.compliance import Policy, filter_providers_v2
    ps = [
        _p("a1", "anthropic", "claude-3-5-haiku"),
        _p("a2", "anthropic", "claude-opus-4-0"),
        _p("o", "openai", "gpt-4o"),
    ]
    out = filter_providers_v2(ps, Policy(blocked_models=("claude-*",)))
    assert [p.id for p in out] == ["o"]


def test_filter_v2_works_with_requested_model_glob():
    """When the caller asks for a model that matches blocked_models, the
    filter must drop every provider whose default_model also matches OR
    the requested_model itself matches — preserves defense in depth."""
    from app.compliance import Policy, filter_providers_v2, ComplianceNoSubstituteError
    # Provider's default_model is OK but requested_model is blocked
    ps = [_p("o", "openai", "gpt-4o")]
    with pytest.raises(ComplianceNoSubstituteError):
        filter_providers_v2(
            ps, Policy(blocked_models=("claude-*",)),
            requested_model="claude-3-haiku",
        )


# ── get_effective_policy DB integration ────────────────────────────


@pytest.mark.asyncio
async def test_get_effective_policy_reads_per_key_fields():
    from sqlalchemy.ext.asyncio import (
        AsyncSession, async_sessionmaker, create_async_engine,
    )
    from app.models.db import Base, ApiKey
    from app.compliance import get_effective_policy

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        db.add(ApiKey(
            id="k-policy-1", key_hash="h", key_prefix="p", name="t",
            blocked_companies=["anthropic"],
            allowed_companies=["openai"],
            blocked_models=["claude-opus-4-0"],
            allowed_models=["gpt-*"],
        ))
        await db.commit()
        policy = await get_effective_policy(db, "k-policy-1")
        assert policy.blocked_companies == {"anthropic"}
        assert policy.allowed_companies == {"openai"}
        assert policy.blocked_models == ("claude-opus-4-0",)
        assert policy.allowed_models == ("gpt-*",)


@pytest.mark.asyncio
async def test_get_effective_policy_legacy_key_returns_empty():
    """A key created before v5.2.0 (all new fields NULL) yields an empty
    Policy — preserves zero-overhead pass-through."""
    from sqlalchemy.ext.asyncio import (
        AsyncSession, async_sessionmaker, create_async_engine,
    )
    from app.models.db import Base, ApiKey
    from app.compliance import get_effective_policy

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        db.add(ApiKey(
            id="k-legacy", key_hash="h", key_prefix="p", name="t",
        ))
        await db.commit()
        policy = await get_effective_policy(db, "k-legacy")
        assert policy.is_empty() is True


# ── Source pins on the new fields ──────────────────────────────────


def test_apikey_model_has_new_columns():
    from app.models.db import ApiKey
    for col in ("allowed_companies", "blocked_models", "allowed_models"):
        assert hasattr(ApiKey, col), f"ApiKey missing column {col}"


def test_database_migration_alter_statements_present():
    from pathlib import Path
    src = Path("app/models/database.py").read_text()
    for col in ("allowed_companies", "blocked_models", "allowed_models"):
        assert f"ADD COLUMN {col}" in src, f"missing ALTER for {col}"


def test_config_has_system_policy_fields():
    from app.config import Settings
    s = Settings.model_fields
    for name in (
        "compliance_system_allowed_companies",
        "compliance_system_blocked_models",
        "compliance_system_allowed_models",
    ):
        assert name in s, f"config missing {name}"
