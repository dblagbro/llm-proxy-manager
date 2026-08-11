"""v5.22.6 regression: _next_route must always terminate.

Production wedge (2026-08-10, tmrwww01 + tmrwww02): _next_route passed a
SINGLE ``exclude_provider_id`` seed picked as ``next(iter(extended_excluded))``.
When select_provider returned a provider that had already been tried, the
"progress" step was ``extended_excluded.add(candidate.provider.id)`` — a no-op,
since the id was already in the set. The seed was then recomputed identically,
so the loop spun forever, each pass calling select_provider (2 DB queries per
provider via _load_profile). A single request that hit a provider error pegged
the event loop and drained the pool to 50/50 on an otherwise idle node.

These tests drive _next_route with a stubbed select_provider and assert it
returns or raises. Against the pre-fix code they hang rather than fail, so each
is wrapped in a hard timeout that converts a spin into a failure.
"""
import asyncio
import sys
import types

import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.routing import fallback as fb  # noqa: E402

TIMEOUT_S = 5.0


class _Provider:
    def __init__(self, pid, provider_type="openai"):
        self.id = pid
        self.name = pid
        self.provider_type = provider_type


class _Route:
    def __init__(self, pid, provider_type="openai"):
        self.provider = _Provider(pid, provider_type)


#: A bounded _next_route calls select_provider at most once per provider. Any
#: run that blows past this is spinning; raise so the test FAILS instead of
#: hanging. (The pre-fix code loops without ever yielding to the event loop,
#: so asyncio.wait_for alone cannot rescue it — hence the hard call cap.)
MAX_CALLS = 200


def _install_select_provider(monkeypatch, responder):
    """Stub select_provider; record every call's exclusion arguments."""
    calls = []

    async def fake_select_provider(db, hint, **kw):
        calls.append(kw)
        if len(calls) > MAX_CALLS:
            raise AssertionError(
                f"select_provider called {len(calls)}x — _next_route is spinning "
                "(this is the v5.22.6 production wedge)"
            )
        # Real select_provider hits the DB; yielding here lets asyncio.wait_for
        # act as a second backstop.
        await asyncio.sleep(0)
        return responder(kw, len(calls))

    monkeypatch.setattr(fb, "select_provider", fake_select_provider)
    return calls


async def _next(tried):
    return await fb._next_route(
        db=None, hint=None, has_tools=False, has_images=False,
        key_type="standard", pinned_provider_id=None, model_override=None,
        tried_ids=set(tried),
    )


@pytest.mark.asyncio
async def test_terminates_when_selector_keeps_returning_tried_provider(monkeypatch):
    """The exact production shape: selector never offers anything fresh.

    Pre-fix this spins forever. Post-fix the full exclusion set is passed, so
    a correct selector raises RuntimeError -> "no untried candidate remains".
    """
    def responder(kw, _n):
        excluded = kw.get("exclude_provider_ids") or set()
        # Honest selector: cannot satisfy the exclusion set.
        if {"A", "B"} <= excluded:
            raise RuntimeError("No provider available after excluding")
        return _Route("A")

    calls = _install_select_provider(monkeypatch, responder)

    with pytest.raises(RuntimeError, match="no untried candidate remains"):
        await asyncio.wait_for(_next({"A", "B"}), timeout=TIMEOUT_S)

    assert len(calls) < 10, f"selector called {len(calls)}x — loop is not bounded"


@pytest.mark.asyncio
async def test_passes_full_exclusion_set_not_a_single_seed(monkeypatch):
    """Root cause: only ONE tried provider was ever excluded per call."""
    def responder(_kw, _n):
        return _Route("C")

    calls = _install_select_provider(monkeypatch, responder)

    result = await asyncio.wait_for(_next({"A", "B"}), timeout=TIMEOUT_S)

    assert result.provider.id == "C"
    assert calls[0].get("exclude_provider_ids") == {"A", "B"}, (
        "must exclude every tried provider in one call"
    )
    assert calls[0].get("exclude_provider_id") is None, (
        "single-seed exclusion is what caused the ping-pong"
    )


@pytest.mark.asyncio
async def test_oauth_skips_still_terminate(monkeypatch):
    """OAuth candidates are skipped by adding to the set — must still converge."""
    seq = [_Route("A", "claude-oauth"), _Route("B", "ChatGPT-oauth-plan"), _Route("C")]

    def responder(_kw, n):
        return seq[min(n - 1, len(seq) - 1)]

    _install_select_provider(monkeypatch, responder)

    result = await asyncio.wait_for(_next({"Z"}), timeout=TIMEOUT_S)
    assert result.provider.id == "C"
    assert result.provider.provider_type == "openai"


@pytest.mark.asyncio
async def test_misbehaving_selector_raises_instead_of_spinning(monkeypatch):
    """Defensive guard: an excluded provider coming back must NOT loop."""
    def responder(_kw, _n):
        return _Route("A")  # already excluded, ignores the contract

    calls = _install_select_provider(monkeypatch, responder)

    with pytest.raises(RuntimeError, match="refusing to loop"):
        await asyncio.wait_for(_next({"A"}), timeout=TIMEOUT_S)

    assert len(calls) == 1, "must fail on the first bad response, not retry"


@pytest.mark.asyncio
async def test_pinned_provider_has_no_fallback(monkeypatch):
    _install_select_provider(monkeypatch, lambda _kw, _n: _Route("C"))

    with pytest.raises(RuntimeError, match="pinned provider has no fallback"):
        await asyncio.wait_for(
            fb._next_route(
                db=None, hint=None, has_tools=False, has_images=False,
                key_type="standard", pinned_provider_id="P1",
                model_override=None, tried_ids={"A"},
            ),
            timeout=TIMEOUT_S,
        )
