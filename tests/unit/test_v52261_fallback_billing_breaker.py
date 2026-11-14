"""v5.22.61 — a billing-class failure inside the fallback chain must open the
failing provider's circuit breaker even when a later provider serves the
request (the handler only records the chain's final outcome)."""

import sys
import types

import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.routing import circuit_breaker, fallback  # noqa: E402
from app.routing.fallback import try_ranked_non_streaming  # noqa: E402

SPEND_LIMIT = "RateLimitError: Your project has reached its configured enforced spend limit"


class _FakeProvider:
    def __init__(self, pid, name):
        self.id = pid
        self.name = name
        self.provider_type = "openai"


class _FakeRoute:
    def __init__(self, pid, name):
        self.provider = _FakeProvider(pid, name)
        self.litellm_model = f"openai/{name}"


ROUTE_A = _FakeRoute("prov-A", "Provider-A")
ROUTE_B = _FakeRoute("prov-B", "Provider-B")


async def _run(monkeypatch, err_a):
    recorded = []

    async def fake_record_failure(provider_id, billing_error=False):
        recorded.append((provider_id, billing_error))

    async def fake_next_route(db, hint, *, tried_ids, **kw):
        if ROUTE_B.provider.id in tried_ids:
            raise RuntimeError("no more candidates")
        return ROUTE_B

    async def call_fn(route):
        if route.provider.id == ROUTE_A.provider.id:
            raise Exception(err_a)
        return "served-by-B"

    monkeypatch.setattr(circuit_breaker, "record_failure", fake_record_failure)
    monkeypatch.setattr(fallback, "_next_route", fake_next_route)
    result, final_route, _chain = await try_ranked_non_streaming(
        db=None,
        hint=None,
        has_tools=False,
        has_images=False,
        key_type="metered",
        pinned_provider_id=None,
        model_override=None,
        primary_route=ROUTE_A,
        call_fn=call_fn,
    )
    assert result == "served-by-B"
    assert final_route.provider.id == ROUTE_B.provider.id
    return recorded


@pytest.mark.asyncio
async def test_billing_failure_absorbed_by_fallback_opens_breaker(monkeypatch):
    recorded = await _run(monkeypatch, SPEND_LIMIT)
    assert recorded == [("prov-A", True)]


@pytest.mark.asyncio
async def test_transient_failure_absorbed_by_fallback_is_not_recorded(monkeypatch):
    recorded = await _run(monkeypatch, "APIConnectionError: connection reset by peer")
    assert recorded == []


@pytest.mark.asyncio
async def test_breaker_bookkeeping_error_does_not_fail_the_request(monkeypatch):
    async def boom(provider_id, billing_error=False):
        raise RuntimeError("breaker store unavailable")

    async def fake_next_route(db, hint, *, tried_ids, **kw):
        if ROUTE_B.provider.id in tried_ids:
            raise RuntimeError("no more candidates")
        return ROUTE_B

    async def call_fn(route):
        if route.provider.id == ROUTE_A.provider.id:
            raise Exception(SPEND_LIMIT)
        return "served-by-B"

    monkeypatch.setattr(circuit_breaker, "record_failure", boom)
    monkeypatch.setattr(fallback, "_next_route", fake_next_route)
    result, _route, _chain = await try_ranked_non_streaming(
        db=None,
        hint=None,
        has_tools=False,
        has_images=False,
        key_type="metered",
        pinned_provider_id=None,
        model_override=None,
        primary_route=ROUTE_A,
        call_fn=call_fn,
    )
    assert result == "served-by-B"
