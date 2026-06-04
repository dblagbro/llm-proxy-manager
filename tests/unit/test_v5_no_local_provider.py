"""v5.0.4 — coordinator-local without a self-hosted provider must 503
with ``X-Compliance-Refusal-Reason: no-compliant-local-provider``
(hub-team-flagged F anomaly on 2026-06-04 smoke matrix run).

The shape is:

- ``ComplianceNoLocalProviderError`` is a subclass of
  ``ComplianceNoSubstituteError`` so legacy catches still see it.
- ``refusal_headers_no_local`` emits the new reason code.
- ``select_provider_with_503`` raises the new error when the
  caller requests ``coordinator-local`` and no provider satisfies
  ``is_self_hosted_provider``.
- messages.py / completions.py catch the more specific error
  BEFORE the generic NoSubstitute one and emit a
  ``compliance_no_local_provider`` audit event.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.compliance import (
    ComplianceNoLocalProviderError,
    ComplianceNoSubstituteError,
    refusal_headers_no_local,
)


@pytest_asyncio.fixture(autouse=True)
async def _isolate_provider_table():
    """Snapshot the providers table before each test that mutates it,
    and restore it on yield. Without this, the tests below blow away
    seed rows that neighboring tests' fixtures depend on."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    async with AsyncSessionLocal() as db:
        before_ids = {p.id for p in (await db.execute(select(Provider))).scalars().all()}
    yield
    async with AsyncSessionLocal() as db:
        for p in (await db.execute(select(Provider))).scalars().all():
            if p.id not in before_ids:
                await db.delete(p)
        await db.commit()


def test_no_local_provider_is_subclass_of_no_substitute():
    """Legacy code that catches ComplianceNoSubstituteError still works."""
    assert issubclass(ComplianceNoLocalProviderError, ComplianceNoSubstituteError)


def test_no_local_error_carries_zero_dropped_count():
    """It's not a 'filtered out by policy' case — no providers were
    dropped, just none qualified as self-hosted."""
    e = ComplianceNoLocalProviderError()
    assert e.n_dropped == 0
    assert e.blocked_companies == set()


def test_refusal_headers_no_local_shape():
    """CADC §6.2 specifies the exact header values."""
    h = refusal_headers_no_local(audit_id="comp_abc")
    assert h["X-Compliance-Refusal"] == "true"
    assert h["X-Compliance-Refusal-Reason"] == "no-compliant-local-provider"
    assert h["X-Compliance-Audit-Id"] == "comp_abc"


@pytest.mark.asyncio
async def test_select_provider_with_503_raises_on_coordinator_local_without_self_hosted():
    """The enforcement integration. With no self-hosted provider in the
    DB, requesting ``coordinator-local`` raises
    ``ComplianceNoLocalProviderError`` BEFORE select_provider runs."""
    from unittest.mock import MagicMock
    from app.api._request_pipeline import select_provider_with_503
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from app.routing.aliases import is_self_hosted_provider

    # If any existing provider satisfies is_self_hosted_provider, the
    # enforcement won't trigger. Probe first; skip if pre-seeded data
    # would mask the assertion. The autouse fixture handles restore
    # (so disabling within this test wouldn't help — easier to skip).
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(Provider))).scalars().all()
        if any(is_self_hosted_provider(p) for p in existing):
            pytest.skip("A self-hosted provider already exists in fixture data")

    parsed_slug = MagicMock()
    parsed_slug.bare_model = "coordinator-local"
    parsed_slug.sort_mode = None
    key_record = MagicMock()
    key_record.id = "k1"
    key_record.key_type = "standard"

    async with AsyncSessionLocal() as db:
        with pytest.raises(ComplianceNoLocalProviderError):
            await select_provider_with_503(
                db, hint=None,
                has_tools=False, has_images=False,
                key_record=key_record, parsed_slug=parsed_slug, alias=None,
            )


@pytest.mark.asyncio
async def test_select_provider_with_503_passes_through_when_self_hosted_exists():
    """When a self-hosted provider IS configured, the coordinator-local
    enforcement does NOT raise — it falls through to select_provider
    (which then runs the LMRH ranker with the self_hosted_only hint)."""
    from unittest.mock import MagicMock
    from app.api._request_pipeline import select_provider_with_503
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider

    # Add a self-hosted provider; the autouse fixture will remove it.
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="nolocal_test_ollama1", name="Local Ollama (test)",
            provider_type="ollama", enabled=True,
            priority=10, default_model="llama3-70b-instruct",
        ))
        await db.commit()

    parsed_slug = MagicMock()
    parsed_slug.bare_model = "coordinator-local"
    parsed_slug.sort_mode = None
    key_record = MagicMock()
    key_record.id = "k1"
    key_record.key_type = "standard"

    async with AsyncSessionLocal() as db:
        # Should NOT raise ComplianceNoLocalProviderError. It may raise
        # other RuntimeError/HTTPException depending on select_provider's
        # downstream behavior, but the new local check is satisfied.
        try:
            await select_provider_with_503(
                db, hint=None,
                has_tools=False, has_images=False,
                key_record=key_record, parsed_slug=parsed_slug, alias=None,
            )
        except ComplianceNoLocalProviderError:
            pytest.fail(
                "select_provider_with_503 should NOT raise when a self-hosted "
                "provider exists"
            )
        except Exception:
            # Downstream failure path is fine — just not the local-provider one.
            pass


@pytest.mark.asyncio
async def test_select_provider_with_503_passes_through_when_owner_company_local():
    """Operator-tagged self-hosted via owner_company='internal' (decision
    29 correction 2026-06-03) — should also satisfy the filter."""
    from unittest.mock import MagicMock
    from app.api._request_pipeline import select_provider_with_503
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider

    # Add a provider tagged via owner_company='internal' (decision 29
    # correction). autouse fixture cleans up on yield.
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="nolocal_test_internal1", name="Internal vLLM (test)",
            provider_type="compatible", enabled=True,
            owner_company="internal",
            priority=10, default_model="custom-llm",
        ))
        await db.commit()

    parsed_slug = MagicMock()
    parsed_slug.bare_model = "coordinator-local"
    parsed_slug.sort_mode = None
    key_record = MagicMock()
    key_record.id = "k1"
    key_record.key_type = "standard"

    async with AsyncSessionLocal() as db:
        try:
            await select_provider_with_503(
                db, hint=None,
                has_tools=False, has_images=False,
                key_record=key_record, parsed_slug=parsed_slug, alias=None,
            )
        except ComplianceNoLocalProviderError:
            pytest.fail(
                "owner_company='internal' should satisfy is_self_hosted_provider"
            )
        except Exception:
            pass
