"""v5.20.3 — Per-request hook override via ``X-Hooks-Override`` header.

Ported pattern from ccproxy (2026-06-30 peer-comparison-roadmap memo,
Section 5 "Absorb from ccproxy"). Sandbox/validation keys with
``debug_echo_enabled=True`` can send:

    X-Hooks-Override: +some_hook,-compliance_substitution_header_hook

to force-enable a degraded hook OR force-disable an otherwise-healthy
one for a single request. Gate: without ``debug_echo_enabled`` the
header is silently ignored (so a compromised regular key can't turn
off compliance hooks).

Response emits ``X-Hooks-Applied`` (which hooks ran) and
``X-Hooks-Skipped`` (which were force-off) when the override
mechanism engaged — for observability.
"""
from __future__ import annotations
from pathlib import Path


def test_parse_empty_returns_empty_sets():
    from app.api._response_hook_runner import _parse_hooks_override_header
    assert _parse_hooks_override_header(None) == (set(), set())
    assert _parse_hooks_override_header("") == (set(), set())
    assert _parse_hooks_override_header(" , , ") == (set(), set())


def test_parse_force_enable_only():
    from app.api._response_hook_runner import _parse_hooks_override_header
    enabled, disabled = _parse_hooks_override_header("+a,+b")
    assert enabled == {"a", "b"}
    assert disabled == set()


def test_parse_force_disable_only():
    from app.api._response_hook_runner import _parse_hooks_override_header
    enabled, disabled = _parse_hooks_override_header("-x,-y")
    assert enabled == set()
    assert disabled == {"x", "y"}


def test_parse_mixed_and_whitespace_tolerant():
    from app.api._response_hook_runner import _parse_hooks_override_header
    enabled, disabled = _parse_hooks_override_header(
        "  +alpha ,  -beta,  +gamma  "
    )
    assert enabled == {"alpha", "gamma"}
    assert disabled == {"beta"}


def test_parse_unknown_prefix_silently_skipped():
    from app.api._response_hook_runner import _parse_hooks_override_header
    enabled, disabled = _parse_hooks_override_header("junk,+valid,also_junk")
    assert enabled == {"valid"}
    assert disabled == set()


def test_parse_bare_plus_or_minus_is_ignored():
    from app.api._response_hook_runner import _parse_hooks_override_header
    enabled, disabled = _parse_hooks_override_header("+,-")
    assert enabled == set()
    assert disabled == set()


class _FakeKey:
    def __init__(self, debug_echo_enabled: bool):
        self.id = "test-key"
        self.debug_echo_enabled = debug_echo_enabled


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def test_extract_returns_empty_when_no_debug_echo():
    from app.api._response_hook_runner import _extract_hooks_override, HookContext
    ctx = HookContext(
        request=_FakeRequest({"X-Hooks-Override": "-compliance_substitution_header_hook"}),
        key_record=_FakeKey(debug_echo_enabled=False),
    )
    enabled, disabled = _extract_hooks_override(ctx)
    assert enabled == set()
    assert disabled == set()


def test_extract_returns_parsed_when_debug_echo():
    from app.api._response_hook_runner import _extract_hooks_override, HookContext
    ctx = HookContext(
        request=_FakeRequest({"X-Hooks-Override": "+foo,-bar"}),
        key_record=_FakeKey(debug_echo_enabled=True),
    )
    enabled, disabled = _extract_hooks_override(ctx)
    assert enabled == {"foo"}
    assert disabled == {"bar"}


def test_extract_returns_empty_when_no_request():
    from app.api._response_hook_runner import _extract_hooks_override, HookContext
    ctx = HookContext(request=None, key_record=_FakeKey(debug_echo_enabled=True))
    enabled, disabled = _extract_hooks_override(ctx)
    assert enabled == set()
    assert disabled == set()


def test_extract_returns_empty_when_no_header():
    from app.api._response_hook_runner import _extract_hooks_override, HookContext
    ctx = HookContext(
        request=_FakeRequest({}),
        key_record=_FakeKey(debug_echo_enabled=True),
    )
    enabled, disabled = _extract_hooks_override(ctx)
    assert enabled == set()
    assert disabled == set()


def test_extract_case_insensitive_header_lookup():
    from app.api._response_hook_runner import _extract_hooks_override, HookContext
    ctx = HookContext(
        request=_FakeRequest({"x-hooks-override": "+foo"}),
        key_record=_FakeKey(debug_echo_enabled=True),
    )
    enabled, disabled = _extract_hooks_override(ctx)
    assert enabled == {"foo"}


def test_runner_skips_force_disabled_hook_when_gate_open():
    import asyncio
    from app.api._response_hook_runner import (
        apply_response_hooks, register_hook, reset_registry_for_tests,
        HookContext,
    )

    async def _ran_hook_fn(*, handler_id, resp_headers, context):
        return {"X-Ran": "yes"}

    reset_registry_for_tests()
    register_hook("ran_hook", _ran_hook_fn, priority=0)

    ctx = HookContext(
        request=_FakeRequest({"X-Hooks-Override": "-ran_hook"}),
        key_record=_FakeKey(debug_echo_enabled=True),
    )
    resp = {}
    asyncio.run(apply_response_hooks(
        handler_id="test", resp_headers=resp, context=ctx,
    ))
    assert "X-Ran" not in resp
    assert resp.get("X-Hooks-Skipped") == "ran_hook"
    reset_registry_for_tests()


def test_runner_ignores_override_when_gate_closed():
    import asyncio
    from app.api._response_hook_runner import (
        apply_response_hooks, register_hook, reset_registry_for_tests,
        HookContext,
    )

    async def _hook_fn(*, handler_id, resp_headers, context):
        return {"X-Ran": "yes"}

    reset_registry_for_tests()
    register_hook("test_hook", _hook_fn, priority=0)

    ctx = HookContext(
        request=_FakeRequest({"X-Hooks-Override": "-test_hook"}),
        key_record=_FakeKey(debug_echo_enabled=False),
    )
    resp = {}
    asyncio.run(apply_response_hooks(
        handler_id="test", resp_headers=resp, context=ctx,
    ))
    assert resp.get("X-Ran") == "yes"
    assert "X-Hooks-Skipped" not in resp
    assert "X-Hooks-Applied" not in resp
    reset_registry_for_tests()


def test_runner_force_enables_degraded_hook():
    """A degraded hook is normally skipped; ``+name`` runs it this once
    (useful for retrying after fixing something upstream)."""
    import asyncio
    from app.api import _response_hook_runner as R

    async def _hook_fn(*, handler_id, resp_headers, context):
        return {"X-Recovered": "yes"}

    R.reset_registry_for_tests()
    R.register_hook("crashy", _hook_fn, priority=0)
    # Access module attribute — reset reassigns _REGISTRY at module scope
    R._REGISTRY[0].degraded = True

    ctx = R.HookContext(
        request=_FakeRequest({"X-Hooks-Override": "+crashy"}),
        key_record=_FakeKey(debug_echo_enabled=True),
    )
    resp = {}
    asyncio.run(R.apply_response_hooks(
        handler_id="test", resp_headers=resp, context=ctx,
    ))
    assert resp.get("X-Recovered") == "yes"
    R.reset_registry_for_tests()


def test_runner_docstring_documents_override():
    src = Path("app/api/_response_hook_runner.py").read_text()
    assert "X-Hooks-Override" in src
    assert "X-Hooks-Applied" in src
    assert "debug_echo_enabled" in src


def test_extract_helper_present():
    src = Path("app/api/_response_hook_runner.py").read_text()
    assert "def _extract_hooks_override" in src
    assert "def _parse_hooks_override_header" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 20, 3), (
        f"expected >= 5.20.3, got {major}.{minor}.{patch}"
    )
