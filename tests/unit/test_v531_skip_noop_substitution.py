"""v5.3.1 — skip the audit row + disclosure when a "substitution" is
actually a no-op (requested_model == served_model).

Observed 2026-06-08 during proactive log review: the hub canary on
each compliance-locked node was emitting ~6 ``model_substitution``
audit rows per 7d where requested_model == served_model
(``gemini-2.5-flash → gemini/gemini-2.5-flash``). The router marked
``compliance_substituted=True`` for normalization reasons, but the
served model is identical to what the caller asked for, so the audit
row is just noise.

This batch adds a defense-in-depth skip at the emit site. Matching
strips the litellm ``provider/`` prefix on the served side so
``anthropic/claude-haiku`` matches ``claude-haiku``; comparison is
case-insensitive.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_route(*, served_model: str, substituted: bool = True):
    return SimpleNamespace(
        compliance_substituted=substituted,
        litellm_model=served_model,
        compliance_blocked_company="anthropic",
        compliance_served_company="google",
        provider=SimpleNamespace(id="prov-test"),
    )


def _make_request():
    req = SimpleNamespace()
    req.headers = {"user-agent": "test"}
    return req


def _make_key():
    return SimpleNamespace(id="key-test")


@pytest.mark.asyncio
async def test_noop_substitution_does_not_emit():
    """When orig_request_model and served_model are the same string,
    the helper returns the empty triple without emitting an audit row.
    """
    from app.api import _compliance_handler as h

    with patch.object(h, "emit_event", new=AsyncMock()) as m:
        headers, disclosure, prelude = await h.emit_substitution_disclosure_for_route(
            _make_request(),
            db=None,
            route=_make_route(served_model="gemini-2.5-flash"),
            key_record=_make_key(),
            orig_request_model="gemini-2.5-flash",
        )
    assert headers == {}
    assert disclosure is None
    assert prelude is False
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_substitution_with_provider_prefix_strips_correctly():
    """The litellm prefix on the served side (e.g. ``gemini/gemini-2.5-flash``)
    must be stripped before equality compare — otherwise the no-op skip
    would never fire on the production path that triggered this batch."""
    from app.api import _compliance_handler as h

    with patch.object(h, "emit_event", new=AsyncMock()) as m:
        headers, disclosure, prelude = await h.emit_substitution_disclosure_for_route(
            _make_request(),
            db=None,
            route=_make_route(served_model="gemini/gemini-2.5-flash"),
            key_record=_make_key(),
            orig_request_model="gemini-2.5-flash",
        )
    assert headers == {}
    assert disclosure is None
    assert prelude is False
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_substitution_still_emits():
    """The skip must NOT fire when requested_model and served_model
    genuinely differ. claude-haiku-4-5 → gemini/gemini-2.5-flash is
    the dominant real substitution pattern on the hub canary."""
    from app.api import _compliance_handler as h

    with patch.object(h, "emit_event", new=AsyncMock()) as m, \
         patch.object(h, "compliance_headers", return_value={"X-Compliance-Substituted": "true"}), \
         patch.object(h, "build_disclosure_payload", return_value={"audit_id": "a"}), \
         patch.object(h, "wants_sse_prelude", return_value=False):
        headers, disclosure, prelude = await h.emit_substitution_disclosure_for_route(
            _make_request(),
            db=None,
            route=_make_route(served_model="gemini/gemini-2.5-flash"),
            key_record=_make_key(),
            orig_request_model="claude-haiku-4-5",
        )
    assert headers == {"X-Compliance-Substituted": "true"}
    assert disclosure == {"audit_id": "a"}
    assert prelude is False
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsubstituted_route_still_short_circuits():
    """The pre-existing early return (compliance_substituted=False)
    still fires, regardless of model equality."""
    from app.api import _compliance_handler as h

    with patch.object(h, "emit_event", new=AsyncMock()) as m:
        headers, disclosure, prelude = await h.emit_substitution_disclosure_for_route(
            _make_request(),
            db=None,
            route=_make_route(served_model="claude-haiku-4-5", substituted=False),
            key_record=_make_key(),
            orig_request_model="claude-haiku-4-5",
        )
    assert headers == {}
    assert disclosure is None
    assert prelude is False
    m.assert_not_awaited()
