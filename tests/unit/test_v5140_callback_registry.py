"""v5.14.0 — Callback registry + migration of X-Compliance-Substitution
emission into a built-in hook.

Closes hub team's 2026-06-30 peer-comparison-roadmap Tier 1 ask.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


# ── (1) Registry surface ───────────────────────────────────────────────


def test_runner_module_imports():
    from app.api._response_hook_runner import (
        register_hook, unregister_hook, registered_hooks,
        apply_response_hooks, reset_registry_for_tests,
        HookContext, register_builtin_hooks,
    )


def test_hook_context_default_fields():
    """Hub-side substitution-mirror hook needs at minimum these fields
    (per 2026-06-30 reply memo)."""
    from app.api._response_hook_runner import HookContext
    ctx = HookContext()
    for f in ("requested_model", "served_model", "api_key_id",
              "provider_id", "compliance_event_id", "substituted",
              "key_record", "request"):
        assert hasattr(ctx, f)


def test_register_then_apply_runs_hook():
    """The basic happy path: register a no-op hook, run the runner,
    verify the hook fires + headers contain its contribution."""
    from app.api._response_hook_runner import (
        register_hook, apply_response_hooks, reset_registry_for_tests,
        HookContext,
    )
    reset_registry_for_tests()
    calls = []

    async def my_hook(*, handler_id, resp_headers, context):
        calls.append((handler_id, context.api_key_id))
        return {"X-My-Hook": "yes"}

    register_hook("my_hook", my_hook)
    headers = {}
    asyncio.run(apply_response_hooks(
        handler_id="messages", resp_headers=headers,
        context=HookContext(api_key_id="k1"),
    ))
    assert headers.get("X-My-Hook") == "yes"
    assert calls == [("messages", "k1")]
    reset_registry_for_tests()


def test_priority_sort_is_stable():
    """Lower priority runs first; ties broken by registration order."""
    from app.api._response_hook_runner import (
        register_hook, registered_hooks, reset_registry_for_tests,
    )
    reset_registry_for_tests()

    async def noop(*, handler_id, resp_headers, context): return None
    register_hook("c", noop, priority=10)
    register_hook("a", noop, priority=0)
    register_hook("b", noop, priority=5)
    names = [h["name"] for h in registered_hooks()]
    assert names == ["a", "b", "c"]
    reset_registry_for_tests()


def test_re_register_replaces_in_place():
    from app.api._response_hook_runner import (
        register_hook, registered_hooks, reset_registry_for_tests,
    )
    reset_registry_for_tests()

    async def v1(*, handler_id, resp_headers, context): return None
    async def v2(*, handler_id, resp_headers, context): return None
    register_hook("h", v1, timeout_sec=5.0)
    register_hook("h", v2, timeout_sec=1.0)
    snap = registered_hooks()
    assert len(snap) == 1
    assert snap[0]["timeout_sec"] == 1.0
    reset_registry_for_tests()


# ── (2) Fail-closed semantics ──────────────────────────────────────────


def test_hook_timeout_emits_failure_header():
    from app.api._response_hook_runner import (
        register_hook, apply_response_hooks, reset_registry_for_tests,
        HookContext,
    )
    reset_registry_for_tests()

    async def slow_hook(*, handler_id, resp_headers, context):
        await asyncio.sleep(10)

    register_hook("slow_hook", slow_hook, timeout_sec=0.05)
    headers = {}
    asyncio.run(apply_response_hooks(
        handler_id="messages", resp_headers=headers,
        context=HookContext(),
    ))
    assert "X-Hook-Failure" in headers
    assert "slow_hook:timeout" in headers["X-Hook-Failure"]
    reset_registry_for_tests()


def test_hook_exception_emits_failure_header():
    from app.api._response_hook_runner import (
        register_hook, apply_response_hooks, reset_registry_for_tests,
        HookContext,
    )
    reset_registry_for_tests()

    async def buggy_hook(*, handler_id, resp_headers, context):
        raise ValueError("boom")

    register_hook("buggy_hook", buggy_hook)
    headers = {}
    asyncio.run(apply_response_hooks(
        handler_id="messages", resp_headers=headers,
        context=HookContext(),
    ))
    assert "X-Hook-Failure" in headers
    assert "buggy_hook:exception:ValueError" in headers["X-Hook-Failure"]
    reset_registry_for_tests()


def test_hook_degrades_after_consecutive_failures():
    """After 5 failures the hook is marked degraded + skipped. This
    prevents one runaway hook from spamming every response."""
    from app.api._response_hook_runner import (
        register_hook, registered_hooks, apply_response_hooks,
        reset_registry_for_tests, HookContext,
    )
    reset_registry_for_tests()

    async def chronic_failure(*, handler_id, resp_headers, context):
        raise RuntimeError("always")

    register_hook("chronic", chronic_failure)
    for _ in range(6):
        asyncio.run(apply_response_hooks(
            handler_id="messages", resp_headers={},
            context=HookContext(),
        ))
    snap = [h for h in registered_hooks() if h["name"] == "chronic"][0]
    assert snap["degraded"] is True
    reset_registry_for_tests()


# ── (3) Built-in compliance substitution hook ─────────────────────────


def test_builtin_substitution_hook_module_present():
    from app.compliance.substitution_hook import (
        compliance_substitution_header_hook,
        _key_has_compliance_policy,
    )


def test_builtin_substitution_hook_emits_true_when_substituted():
    from app.api._response_hook_runner import HookContext
    from app.compliance.substitution_hook import (
        compliance_substitution_header_hook,
    )
    headers: dict = {}
    result = asyncio.run(compliance_substitution_header_hook(
        handler_id="messages", resp_headers=headers,
        context=HookContext(substituted=True),
    ))
    assert result == {"X-Compliance-Substitution": "true"}


def test_builtin_substitution_hook_emits_pass_through_when_no_policy():
    from app.api._response_hook_runner import HookContext
    from app.compliance.substitution_hook import (
        compliance_substitution_header_hook,
    )
    headers: dict = {}
    result = asyncio.run(compliance_substitution_header_hook(
        handler_id="messages", resp_headers=headers,
        context=HookContext(substituted=False, key_record=None),
    ))
    assert result == {"X-Compliance-Substitution": "pass-through"}


def test_builtin_substitution_hook_is_idempotent():
    """If something already set the header, the hook MUST NOT overwrite
    — preserves the v5.9.3 contract that substitution-active emission
    upstream wins."""
    from app.api._response_hook_runner import HookContext
    from app.compliance.substitution_hook import (
        compliance_substitution_header_hook,
    )
    headers = {"X-Compliance-Substitution": "true"}
    result = asyncio.run(compliance_substitution_header_hook(
        handler_id="messages", resp_headers=headers,
        context=HookContext(substituted=False),
    ))
    assert result is None  # no change
    assert headers["X-Compliance-Substitution"] == "true"


# ── (4) Wire-up checks (static-grep, pins the LiteLLM #27518 class) ───


def test_runner_wired_into_messages_handler():
    # v5.19.0 — messages.py tail extracted to _messages_response_tail.py.
    # The wire IS still there, just one file over. Same intent.
    files = [
        Path("app/api/messages.py"),
        Path("app/api/_messages_response_tail.py"),
    ]
    src = "\n".join(f.read_text() for f in files if f.exists())
    assert "from app.api._response_hook_runner import apply_response_hooks, HookContext" in src
    assert 'handler_id="messages"' in src


def test_runner_wired_into_completions_handler():
    src = Path("app/api/completions.py").read_text()
    assert "from app.api._response_hook_runner import apply_response_hooks, HookContext" in src
    assert 'handler_id="completions"' in src


def test_register_builtin_hooks_wired_in_main():
    src = Path("app/main.py").read_text()
    assert "register_builtin_hooks" in src


# ── (5) Settings ───────────────────────────────────────────────────────


def test_settings_exposed():
    from app.config import settings
    assert hasattr(settings, "callbacks_fail_closed")
    assert hasattr(settings, "callbacks_default_timeout_sec")
    assert settings.callbacks_fail_closed is True
    assert settings.callbacks_default_timeout_sec == 2.0


# ── (6) Version ────────────────────────────────────────────────────────


def test_version_bumped():
    """v5.14.0 was the ship that introduced the runner. Assert version is
    at or beyond 5.14 — an earlier version indicates a rollback that
    would break the surface these tests protect."""
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, f"could not parse __version__ from {src!r}"
    major, minor = int(m.group(1)), int(m.group(2))
    assert (major, minor) >= (5, 14), f"expected >= 5.14, got {major}.{minor}"
