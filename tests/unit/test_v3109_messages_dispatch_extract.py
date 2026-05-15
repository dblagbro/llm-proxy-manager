"""v3.10.9 — extract the claude-oauth dispatch chain from messages.py.

messages.py's ``messages()`` handler had grown to a ~913-line function
(file 1002 lines). Its deepest branch — the claude-oauth provider-chain
walk (streaming / non-streaming dispatch + 401-refresh fallback) — was
extracted to ``app/api/_messages_dispatch.py`` as
``dispatch_claude_oauth_chain()``. A pure behavior-preserving move; the
chain-walk helper ``_select_excluding`` moved with it.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _FakeProvider:
    provider_type = "openai"
    id = "fake-prov"
    name = "fake"


class _FakeRoute:
    provider = _FakeProvider()


@pytest.mark.asyncio
async def test_non_claude_oauth_route_falls_through():
    """When the route is not claude-oauth the while-loop never runs —
    the function returns ``(None, route)`` so the caller falls through
    to the litellm path with the route unchanged. Other args are never
    touched, so passing None for db/key_record is safe here."""
    from app.api._messages_dispatch import dispatch_claude_oauth_chain
    route = _FakeRoute()
    resp, out_route = await dispatch_claude_oauth_chain(
        route, body={}, db=None, key_record=None, resp_headers={},
        stream=False, max_tokens=16, llm_hint=None, hint=None,
        has_tools=False, has_images=False,
        conversation_id=None, memory_tag=None,
    )
    assert resp is None
    assert out_route is route


def test_dispatch_module_exposes_helpers():
    from app.api import _messages_dispatch
    assert hasattr(_messages_dispatch, "dispatch_claude_oauth_chain")
    assert hasattr(_messages_dispatch, "_select_excluding")


def test_messages_py_delegates_to_dispatch_module():
    src = Path("app/api/messages.py").read_text()
    # The inline claude-oauth while-block is gone.
    assert 'while route.provider.provider_type == "claude-oauth":' not in src
    # messages.py now delegates to the extracted orchestrator.
    assert "dispatch_claude_oauth_chain(" in src
    assert "from app.api._messages_dispatch import" in src
    # _select_excluding is no longer DEFINED here — moved, only imported.
    assert "async def _select_excluding(" not in src


def test_select_excluding_moved_into_dispatch_module():
    src = Path("app/api/_messages_dispatch.py").read_text()
    assert "async def _select_excluding(" in src
    assert "async def dispatch_claude_oauth_chain(" in src
