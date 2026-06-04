"""v5.0.1 regression — Bedrock-served Anthropic models are tagged as
BOTH ``anthropic`` and ``aws`` (decision 11 in
docs/5.0-compliance-design.md).

Original v5.0.0 bug surfaced during the smoke matrix run:
``model_family_to_company('anthropic.claude-3-haiku-v1:0')`` returned
``'aws'`` only — because the AWS company's ``model_prefixes`` listed
``anthropic.claude-`` and the lookup short-circuited on first match.
This meant banning ``anthropic`` did NOT drop a Bedrock-Anthropic
provider, defeating the spec's "model-family is source of truth for
lineage" rule.

Fix (committed 2026-06-03):
- Added ``anthropic.claude-`` to Anthropic's ``model_prefixes`` list.
- New ``model_family_companies`` returns the SET of every matching
  company (not just the first).
- ``filter_providers`` uses the set-membership check so banning either
  ``anthropic`` or ``aws`` drops the Bedrock provider.

These tests pin that contract so a future refactor of the company map
can't silently regress this multi-company semantics.
"""
import pytest

from app.compliance import (
    ComplianceNoSubstituteError,
    filter_providers,
)
from app.compliance.company_map import (
    KNOWN_COMPANIES,
    model_family_companies,
    model_family_to_company,
)


class _Provider:
    """Minimal stand-in for the ORM Provider in filter_providers tests."""

    def __init__(self, owner_company, default_model):
        self.owner_company = owner_company
        self.default_model = default_model


# ── Company map contract ───────────────────────────────────────────────


def test_anthropic_prefixes_include_bedrock_form():
    """Anthropic's model_prefixes MUST include 'anthropic.claude-' so the
    multi-company match below works. A future contributor moving the
    prefix to AWS only is the regression this test catches."""
    assert "anthropic.claude-" in KNOWN_COMPANIES["anthropic"]["model_prefixes"]


def test_aws_prefixes_include_bedrock_form():
    """AWS's model_prefixes also include 'anthropic.claude-' — banning
    AWS should drop Bedrock-Anthropic too."""
    assert "anthropic.claude-" in KNOWN_COMPANIES["aws"]["model_prefixes"]


def test_model_family_companies_returns_both_for_bedrock_anthropic():
    """The core multi-company invariant. ``anthropic.claude-3-haiku-...``
    is BOTH an Anthropic model (by lineage) AND an AWS-Bedrock model
    (by where it's served). Banning either company drops it."""
    companies = model_family_companies("anthropic.claude-3-haiku-20240307-v1:0")
    assert companies == {"anthropic", "aws"}


def test_model_family_companies_returns_aws_only_for_titan():
    """Sanity check the multi-match isn't over-eager. AWS Titan models
    are NOT Anthropic models — they should tag only as AWS (and Amazon
    via the amazon.titan- prefix in Amazon's list)."""
    companies = model_family_companies("amazon.titan-text-express-v1")
    # AWS and Amazon both include amazon.titan-
    assert "aws" in companies
    assert "amazon" in companies
    assert "anthropic" not in companies


def test_model_family_to_company_single_value_for_bedrock_anthropic():
    """The single-string convenience accessor returns the FIRST match
    (Anthropic, by KNOWN_COMPANIES iteration order). Disclosure
    surfaces use this when they need a single label; the filter
    decision uses the SET form above."""
    assert model_family_to_company("anthropic.claude-3-haiku-v1:0") == "anthropic"


# ── Filter behavior ────────────────────────────────────────────────────


def test_banning_anthropic_drops_bedrock_anthropic_provider():
    """The core fix. v5.0.0 originally returned the Bedrock provider
    through the filter when ``anthropic`` was banned — v5.0.1+ drops
    it via the model_family_companies set check."""
    bedrock = _Provider(
        owner_company="aws",
        default_model="anthropic.claude-3-haiku-20240307-v1:0",
    )
    openai = _Provider(owner_company="openai", default_model="gpt-4o")

    out = filter_providers([bedrock, openai], {"anthropic"})

    assert len(out) == 1
    assert out[0] is openai


def test_banning_aws_also_drops_bedrock_anthropic_provider():
    """The orthogonal cover — banning AWS hits Bedrock-Anthropic via the
    aws side of the multi-company match (and via owner_company too)."""
    bedrock = _Provider(
        owner_company="aws",
        default_model="anthropic.claude-3-haiku-20240307-v1:0",
    )
    openai = _Provider(owner_company="openai", default_model="gpt-4o")

    out = filter_providers([bedrock, openai], {"aws"})

    assert len(out) == 1
    assert out[0] is openai


def test_banning_openai_leaves_bedrock_anthropic_provider():
    """Negative control — an unrelated ban must NOT drop Bedrock-Anthropic."""
    bedrock = _Provider(
        owner_company="aws",
        default_model="anthropic.claude-3-haiku-20240307-v1:0",
    )
    openai = _Provider(owner_company="openai", default_model="gpt-4o")

    out = filter_providers([bedrock, openai], {"openai"})

    assert len(out) == 1
    assert out[0] is bedrock


def test_banning_both_anthropic_and_aws_drops_only_bedrock_anthropic():
    """Compound ban — anthropic + aws together drop Bedrock-Anthropic
    AND any AWS-owned non-Anthropic provider, but leave others."""
    bedrock_anth = _Provider(
        owner_company="aws",
        default_model="anthropic.claude-3-haiku-v1:0",
    )
    bedrock_titan = _Provider(
        owner_company="aws", default_model="amazon.titan-text-express-v1",
    )
    openai = _Provider(owner_company="openai", default_model="gpt-4o")

    out = filter_providers([bedrock_anth, bedrock_titan, openai], {"anthropic", "aws"})

    assert len(out) == 1
    assert out[0] is openai


def test_banning_anthropic_with_only_bedrock_anthropic_raises_no_substitute():
    """If the ONLY provider is Bedrock-Anthropic and Anthropic is
    banned, no substitute is possible → ComplianceNoSubstituteError
    (HTTP 503 path)."""
    bedrock = _Provider(
        owner_company="aws",
        default_model="anthropic.claude-3-haiku-v1:0",
    )

    with pytest.raises(ComplianceNoSubstituteError):
        filter_providers([bedrock], {"anthropic"})
