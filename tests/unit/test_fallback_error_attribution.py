"""Regression: the fallback chain must attribute the final exception to the
provider that actually raised it, not the stale primary.

BUG: on a request where the primary provider fails and the ordered fallback
chain falls through to a DIFFERENT provider that raises the final exception,
the single ``llm_request`` ERROR activity_log row was written with the PRIMARY
provider's ``provider_id`` + ``event_meta.provider_name`` while
``event_meta.error`` carried the LAST (actual failing) provider's text —
provider_id and error disagreed.

Fix: ``try_ranked_non_streaming`` attaches the failing RouteResult to the
raised exception as ``_llmproxy_attributed_route``; the messages/completions
handlers resolve ``failed_route = getattr(e, "_llmproxy_attributed_route",
None) or route`` and pass THAT to ``record_outcome``.
"""
import sys
import types

import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.routing import fallback  # noqa: E402
from app.routing.fallback import try_ranked_non_streaming  # noqa: E402


class _FakeProvider:
    def __init__(self, pid, name):
        self.id = pid
        self.name = name
        # must NOT be an oauth type or the chain skips call_fn entirely
        self.provider_type = "openai"


class _FakeRoute:
    def __init__(self, pid, name, litellm_model):
        self.provider = _FakeProvider(pid, name)
        self.litellm_model = litellm_model


# Two distinct routes with distinct provider id/name and litellm_model.
ROUTE_A = _FakeRoute("prov-A", "Provider-A", "openai/a-model")
ROUTE_B = _FakeRoute("prov-B", "Provider-B", "openai/b-model")

# Distinct, non-same-provider-retriable errors for each (so the chain falls
# through from A and then raises B's error).
ERR_A = "litellm.AuthenticationError: A bad key"
ERR_B = "litellm.BadRequestError: B context length exceeded"


@pytest.mark.asyncio
async def test_final_exception_attributed_to_failing_route(monkeypatch):
    """When A fails and the chain falls through to B which also fails, the
    raised exception is B's AND carries ``_llmproxy_attributed_route == B``."""

    async def fake_next_route(db, hint, *, tried_ids, **kw):
        # First fall-through hands back B; after B is tried there are no more.
        if ROUTE_B.provider.id in tried_ids:
            raise RuntimeError("no more candidates")
        return ROUTE_B

    monkeypatch.setattr(fallback, "_next_route", fake_next_route)

    async def call_fn(route):
        if route.provider.id == ROUTE_A.provider.id:
            raise Exception(ERR_A)
        raise Exception(ERR_B)

    with pytest.raises(Exception) as excinfo:
        await try_ranked_non_streaming(
            db=None, hint=None,
            has_tools=False, has_images=False,
            key_type="metered", pinned_provider_id=None, model_override=None,
            primary_route=ROUTE_A, call_fn=call_fn,
        )

    exc = excinfo.value
    # It raised B's exception (the last, real failure) ...
    assert str(exc) == ERR_B
    # ... and attributed it to B, not the stale primary A.
    attributed = getattr(exc, "_llmproxy_attributed_route", None)
    assert attributed is not None
    assert attributed.provider.id == ROUTE_B.provider.id


@pytest.mark.asyncio
async def test_handler_resolution_record_outcome_agrees(monkeypatch):
    """Focused analogue of the handler's attribution: provider_id and error
    text passed to record_outcome must agree (both B's)."""

    async def fake_next_route(db, hint, *, tried_ids, **kw):
        if ROUTE_B.provider.id in tried_ids:
            raise RuntimeError("no more candidates")
        return ROUTE_B

    monkeypatch.setattr(fallback, "_next_route", fake_next_route)

    async def call_fn(route):
        if route.provider.id == ROUTE_A.provider.id:
            raise Exception(ERR_A)
        raise Exception(ERR_B)

    # ``route`` is the primary the handler starts with.
    route = ROUTE_A
    captured = {}

    async def fake_record_outcome(db, provider_id, litellm_model, **kw):
        captured["provider_id"] = provider_id
        captured["litellm_model"] = litellm_model
        captured["error_str"] = kw.get("error_str")
        captured["provider_name"] = kw.get("provider_name")

    try:
        await try_ranked_non_streaming(
            db=None, hint=None,
            has_tools=False, has_images=False,
            key_type="metered", pinned_provider_id=None, model_override=None,
            primary_route=route, call_fn=call_fn,
        )
    except Exception as e:
        # Exactly what messages.py / completions.py now do.
        failed_route = getattr(e, "_llmproxy_attributed_route", None) or route
        await fake_record_outcome(
            None, failed_route.provider.id, failed_route.litellm_model,
            error_str=str(e), provider_name=failed_route.provider.name,
        )

    # provider_id and error agree — both B's.
    assert captured["provider_id"] == ROUTE_B.provider.id
    assert captured["provider_name"] == ROUTE_B.provider.name
    assert captured["litellm_model"] == ROUTE_B.litellm_model
    assert captured["error_str"] == ERR_B
