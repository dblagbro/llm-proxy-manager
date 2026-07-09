"""v5.8.5 — regression test for the codex-oauth refresh single-flight lock.

Before v5.8.5, ``codex_oauth_flow.refresh_and_persist`` had no
concurrency guard. The proactive expiry sweep
(``cursor_oauth_expiry_monitor``) and the lazy-on-401 path
(``scanner._fetch_codex_oauth_models``, dispatch) could call it
concurrently for the same provider; OpenAI rotates ``refresh_token`` on
every use, so the loser saw ``refresh_token_reused``. The 2026-06-20
smoke incident was this.

This test verifies:
  - ``_get_refresh_lock`` returns the same Lock instance for the same
    provider id (so concurrent callers actually serialize).
  - Different provider ids get different locks (no cross-provider
    head-of-line blocking).
  - ``_verify_oauth_access_token`` exists and returns a bool (the
    contract the refresh path relies on after peer-adopt).
"""
from __future__ import annotations

import asyncio
import pytest


def test_refresh_lock_is_per_provider_singleton():
    from app.providers.codex_oauth_flow import _get_refresh_lock
    lock_a1 = _get_refresh_lock("prov-A")
    lock_a2 = _get_refresh_lock("prov-A")
    lock_b1 = _get_refresh_lock("prov-B")
    assert lock_a1 is lock_a2, (
        "same provider id must return the same Lock — otherwise the "
        "single-flight property is broken and the race v5.8.5 fixed "
        "comes back."
    )
    assert lock_a1 is not lock_b1, (
        "different provider ids must have independent locks — otherwise "
        "provider A blocks provider B's refresh."
    )
    assert isinstance(lock_a1, asyncio.Lock)


def test_verify_helper_exists_and_is_async():
    from app.providers.codex_oauth_flow import _verify_oauth_access_token
    import inspect
    assert inspect.iscoroutinefunction(_verify_oauth_access_token), (
        "_verify_oauth_access_token must be async — the refresh path "
        "awaits it after peer-adopt."
    )


@pytest.mark.asyncio
async def test_verify_returns_true_on_network_error():
    """Verify never raises — only network 401 from chatgpt.com counts as
    a refusal verdict. This is intentional so transient errors don't
    falsely clear a healthy refresh_token."""
    from app.providers import codex_oauth_flow

    async def boom(*a, **kw):
        raise RuntimeError("simulated network failure")

    # Stub the import inside the function so we don't actually hit the
    # network. The function catches any exception and returns True.
    import httpx
    orig_client = httpx.AsyncClient

    class _NoNetClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **kw): raise RuntimeError("no net")

    httpx.AsyncClient = _NoNetClient
    try:
        verdict = await codex_oauth_flow._verify_oauth_access_token("token-xxx")
        assert verdict is True, (
            "network errors must return True so we don't falsely revoke "
            "a healthy refresh_token. Only an explicit 401 from "
            "chatgpt.com is a refusal."
        )
    finally:
        httpx.AsyncClient = orig_client
