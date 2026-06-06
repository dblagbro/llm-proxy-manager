"""v5.0.23 / remediation Batch 2.5 — grok-web → OpenRouter failover.

When the grok-web dispatcher hits a failover-eligible error
(GrokWebError mapping to 502 or 429), it now:

  1. Records the outcome via ``_record_grok_outcome`` (unchanged).
  2. Trips the circuit breaker via ``record_failure`` (NEW).
  3. Returns ``None`` instead of raising ``HTTPException`` (NEW).

The caller (``messages.py`` / ``completions.py``) checks for None,
re-resolves a route excluding the failed grok-web provider, and falls
through to the litellm dispatch path with a header
``X-Grok-Web-Failover: true``.

GrokWebAuthError (401) still raises — that's an operator-attention
condition (bridge needs re-login), not a transient/failover case.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Source pins ─────────────────────────────────────────────────────


def test_dispatcher_has_trip_helper():
    """The new _trip_cb_for_failover helper exists and is callable."""
    from app.api._grok_web_dispatch import _trip_cb_for_failover
    assert callable(_trip_cb_for_failover)


def test_dispatcher_returns_none_on_grokweberror():
    """Confirm the GrokWebError catches in both dispatchers return
    None instead of raising HTTPException (v5.0.23 contract).
    """
    src = Path("app/api/_grok_web_dispatch.py").read_text()
    # 4 catches: openai-stream, openai-nonstream, anthropic-stream,
    # anthropic-nonstream. Each must `return None` after the
    # _trip_cb_for_failover call. We check for the specific marker
    # pattern.
    expected_marker = "await _trip_cb_for_failover("
    assert src.count(expected_marker) >= 4, (
        f"expected ≥4 calls to _trip_cb_for_failover in dispatcher "
        f"(one per GrokWebError catch site); got "
        f"{src.count(expected_marker)}"
    )
    # And confirm no longer raises HTTPException after the trip
    # (sanity check — would be a regression if a raise crept back in).
    return_nones = src.count("return None  # caller: try next provider")
    assert return_nones >= 4, (
        "expected ≥4 'return None' sites with the failover comment"
    )


def test_messages_handles_grok_web_none_and_failovers():
    """messages.py grok-web dispatch site must check for None return
    + re-resolve route via select_provider(exclude_provider_id=...)."""
    src = Path("app/api/messages.py").read_text()
    assert "if gw_resp is not None:" in src
    assert "exclude_provider_id=failed_id" in src
    assert 'X-Grok-Web-Failover' in src


def test_completions_handles_grok_web_none_and_failovers():
    """completions.py mirrors messages.py for the OpenAI shape."""
    src = Path("app/api/completions.py").read_text()
    assert "if gw_resp is not None:" in src
    assert "exclude_provider_id=failed_id" in src
    assert 'X-Grok-Web-Failover' in src


def test_grokwebautherror_still_raises():
    """401 / auth errors are operator-attention; must still raise so
    the operator sees the bridge needs re-login. Failover is the wrong
    response (re-login on the bridge is the right one)."""
    src = Path("app/api/_grok_web_dispatch.py").read_text()
    # Each GrokWebAuthError block ends with `raise HTTPException(401, ...)`.
    # Confirm we didn't accidentally replace those with returns.
    assert src.count("raise HTTPException(401") >= 4, (
        "GrokWebAuthError catches must STILL raise HTTPException(401); "
        "do NOT make these failover-eligible."
    )


# ── Behavioral ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_returns_none_for_failover_eligible(monkeypatch):
    from types import SimpleNamespace
    from app.providers.grok_web import GrokWebError
    from app.api._grok_web_dispatch import dispatch_grok_web_openai

    async def fake_complete(*a, **kw):
        raise GrokWebError("upstream 502", status_code=502)

    async def fake_record(*a, **kw):
        pass

    async def fake_trip(*a, **kw):
        pass

    monkeypatch.setattr("app.providers.grok_web.complete_grok_web", fake_complete)
    monkeypatch.setattr("app.monitoring.helpers.record_outcome", fake_record)
    monkeypatch.setattr(
        "app.routing.circuit_breaker.record_failure", fake_trip
    )

    route = SimpleNamespace(
        provider=SimpleNamespace(
            id="p1", name="Grok-Web-Devin", provider_type="grok-web",
            extra_config={"bridge_url": "http://b", "conversation_id": "c"},
        ),
        profile=SimpleNamespace(model_id="grok-3"),
    )

    import time as _t
    result = await dispatch_grok_web_openai(
        route=route,
        body={"model": "grok-3", "messages": [{"role": "user", "content": "x"}]},
        stream=False, resp_headers={},
        db="fake", key_record_id="k", t0=_t.monotonic(),
    )
    assert result is None
