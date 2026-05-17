"""Capability-fit routing gate (v4.1) — "simulate if we can, skip if we can't".

The gate skips a provider that cannot serve a required capability even with
emulation: vision for an image request, the tools+reasoning emulation
collision, or a context window smaller than the request.
"""
from __future__ import annotations

import dataclasses

from app.routing.router import _capability_fit, RouteResult
from app.routing.lmrh.types import CapabilityProfile


def _profile(**kw) -> CapabilityProfile:
    base = dict(provider_id="p1", provider_type="openai", model_id="m1")
    base.update(kw)
    return CapabilityProfile(**base)


def _fit(p, *, tools=False, reasoning=False, images=False, tokens=None):
    return _capability_fit(p, has_tools=tools, needs_reasoning=reasoning,
                           has_images=images, est_input_tokens=tokens)


# ── vision ───────────────────────────────────────────────────────────────────

def test_vision_request_skips_non_vision_provider():
    assert _fit(_profile(native_vision=False), images=True) is not None


def test_vision_request_ok_on_vision_provider():
    assert _fit(_profile(native_vision=True), images=True) is None


def test_no_images_means_vision_is_irrelevant():
    assert _fit(_profile(native_vision=False), images=False) is None


# ── tools + reasoning collision ──────────────────────────────────────────────

def test_tools_plus_reasoning_not_skipped_native_in_neither():
    # v4.1.1 — co-emulation serves both; a provider native in NEITHER is
    # no longer skipped (the old tools+reasoning 'collision' is gone).
    assert _fit(_profile(native_tools=False, native_reasoning=False),
                tools=True, reasoning=True) is None


def test_tools_plus_reasoning_ok_when_native_tools():
    # native tools -> CoT-E handles reasoning, no collision
    assert _fit(_profile(native_tools=True, native_reasoning=False),
                tools=True, reasoning=True) is None


def test_tools_plus_reasoning_ok_when_native_reasoning():
    # native reasoning -> CoT-E not needed, tool emulation engages
    assert _fit(_profile(native_tools=False, native_reasoning=True),
                tools=True, reasoning=True) is None


def test_tools_without_reasoning_need_is_not_a_collision():
    # tools alone are emulable — only the tools+reasoning pair collides
    assert _fit(_profile(native_tools=False, native_reasoning=False),
                tools=True, reasoning=False) is None


# ── context window ───────────────────────────────────────────────────────────

def test_context_overflow_skips_provider():
    r = _fit(_profile(context_length=128000), tokens=200000)
    assert r is not None and "context" in r


def test_context_within_window_is_ok():
    assert _fit(_profile(context_length=128000), tokens=5000) is None


def test_context_gate_skipped_when_no_estimate():
    # no token estimate -> don't guess, don't gate
    assert _fit(_profile(context_length=8000), tokens=None) is None


# ── clean pass + wiring ──────────────────────────────────────────────────────

def test_fully_capable_provider_passes_everything():
    p = _profile(native_tools=True, native_reasoning=True, native_vision=True,
                 context_length=200000)
    assert _fit(p, tools=True, reasoning=True, images=True, tokens=50000) is None


def test_routeresult_carries_capability_skipped():
    fields = {f.name for f in dataclasses.fields(RouteResult)}
    assert "capability_skipped" in fields
    # default is an empty list, not a shared mutable
    a, b = RouteResult.__dataclass_fields__["capability_skipped"], None
    assert a.default_factory is list
