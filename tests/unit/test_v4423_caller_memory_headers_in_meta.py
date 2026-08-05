"""v4.4.23 — per-event caller-memory header capture in activity_log.

Surfaced 2026-05-27 by a DevinGPT follow-up: they asked us to confirm
whether two specific 2026-05-17 events carried ``X-Conversation-Id``.
We couldn't — activity_log's ``event_meta`` only stored body-derived
fields (model, in_tok, request_preview, …) and never captured request
headers. The Prometheus counter (F-OBS-003, v4.4.15) does, but it's
in-process and resets on container restart, so it can't answer
"did this specific historical event carry the header."

v4.4.23 closes the gap with two new contextvars in
``app/observability/request_context.py`` set at the entry points of
``/v1/messages`` and ``/v1/completions`` and read by
``_build_outcome_meta`` so every activity_log row records whether
``X-Conversation-Id`` and ``X-Memory-Tag`` were present. Only the
boolean is stored — never the value, which can be a privacy-sensitive
client identifier.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Source guards ────────────────────────────────────────────────────


def test_request_context_has_caller_memory_setter():
    src = Path("app/observability/request_context.py").read_text()
    assert "set_caller_memory_headers" in src, "setter missing"
    assert "get_had_x_conversation_id" in src, "getter missing"
    assert "get_had_x_memory_tag" in src, "memory_tag getter missing"
    # ContextVar default must be False so probes / internal calls
    # (which never set it) don't accidentally show up as had=True
    assert '"had_x_conversation_id", default=False' in src


def test_messages_entry_point_sets_contextvar():
    from tests._entry_surface import entry_surface
    src = entry_surface("app/api/messages.py")  # setter lives in _handler_shared, called at entry
    assert "set_caller_memory_headers" in src, (
        "messages.py must call the contextvar setter at entry"
    )
    # Must be in the function body, near the prometheus counter call
    cnt_idx = src.index("CONVERSATION_ID_REQUESTS_TOTAL")
    block = src[cnt_idx:cnt_idx + 1500]
    assert "set_caller_memory_headers" in block, (
        "setter must run alongside the prometheus counter — both rely "
        "on x_conversation_id being read from the Header() param"
    )


def test_completions_entry_point_sets_contextvar():
    from tests._entry_surface import entry_surface
    src = entry_surface("app/api/completions.py")
    assert "set_caller_memory_headers" in src
    cnt_idx = src.index("CONVERSATION_ID_REQUESTS_TOTAL")
    block = src[cnt_idx:cnt_idx + 1500]
    assert "set_caller_memory_headers" in block


def test_build_event_meta_emits_header_flags():
    """Source guard that the activity_log row gets stamped with the
    header presence — this is the whole point of v4.4.23."""
    src = Path("app/monitoring/helpers.py").read_text()
    # Function is named ``_build_event_meta_base`` (was previously
    # private ``_build_outcome_meta`` — the rename happened earlier).
    idx = src.index("def _build_event_meta_base")
    block = src[idx:idx + 4000]
    assert "had_x_conversation_id" in block, (
        "_build_event_meta_base must stamp had_x_conversation_id"
    )
    assert "had_x_memory_tag" in block, (
        "_build_event_meta_base must stamp had_x_memory_tag"
    )
    # Must read from the contextvar, not require plumbing through
    # record_outcome's ~14 call sites (the deliberate v4.4.23 design)
    assert "get_had_x_conversation_id" in block


def test_header_flags_are_bool_only_not_values():
    """We must NEVER persist the conversation_id value itself —
    it's a client identifier. Only the boolean."""
    src = Path("app/monitoring/helpers.py").read_text()
    idx = src.index("def _build_event_meta_base")
    block = src[idx:idx + 4000]
    # The stamp pattern matches the v3.6.2 ``meta["probe"] = True``
    # convention: assigns the literal True, not the header value.
    assert 'meta["had_x_conversation_id"] = True' in block, (
        "presence flag must be the literal True, NOT the header value"
    )


def test_telemetry_failure_does_not_break_request():
    """The contextvar setter is wrapped in try/except at both entry
    points so a bug in the instrumentation can never 500 a real
    request."""
    from tests._entry_surface import entry_surface
    for path in ("app/api/messages.py", "app/api/completions.py"):
        src = entry_surface(path)  # setter + its try/except live in _handler_shared
        idx = src.index("set_caller_memory_headers")
        # Look BEFORE the call for a try:
        prefix = src[max(0, idx - 500):idx]
        assert "try:" in prefix, f"{path}: setter not wrapped in try"
        # And after for the except
        suffix = src[idx:idx + 500]
        assert "except" in suffix, f"{path}: setter missing except block"


# ── Behavioral tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contextvar_default_is_false_outside_request():
    """Probes + internal calls don't run inside a request scope.
    Reading the contextvar must default to False so those events
    aren't stamped with a misleading ``had_x_conversation_id``."""
    from app.observability.request_context import (
        get_had_x_conversation_id, get_had_x_memory_tag,
    )
    # Fresh test scope — no setter has run
    assert get_had_x_conversation_id() is False
    assert get_had_x_memory_tag() is False


@pytest.mark.asyncio
async def test_setter_round_trips():
    from app.observability.request_context import (
        set_caller_memory_headers, get_had_x_conversation_id,
        get_had_x_memory_tag,
    )
    set_caller_memory_headers(has_conversation_id=True, has_memory_tag=False)
    assert get_had_x_conversation_id() is True
    assert get_had_x_memory_tag() is False
    set_caller_memory_headers(has_conversation_id=False, has_memory_tag=True)
    assert get_had_x_conversation_id() is False
    assert get_had_x_memory_tag() is True


@pytest.mark.asyncio
async def test_setter_coerces_truthy_to_bool():
    """Defensive — bare ``str`` values from FastAPI's Header() param
    should be coerced to bool by the setter so we never persist a
    non-bool by accident."""
    from app.observability.request_context import (
        set_caller_memory_headers, get_had_x_conversation_id,
    )
    set_caller_memory_headers(has_conversation_id="chat-abc123")  # type: ignore[arg-type]
    v = get_had_x_conversation_id()
    assert v is True and isinstance(v, bool), (
        f"setter must coerce to True bool, got {v!r}"
    )
    set_caller_memory_headers(has_conversation_id=None)  # type: ignore[arg-type]
    v = get_had_x_conversation_id()
    assert v is False and isinstance(v, bool)
