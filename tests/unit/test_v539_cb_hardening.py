"""v5.3.9 — CB lifecycle hardening tests.

Three changes verified:
1. is_caller_side_error classifier — recognizes the failure modes
   that are caller-side malformed-body bugs (orphan tool_call_id,
   cursor-bridge string-expected, OpenAI Invalid user message).
2. record_outcome skips CB increment when caller-side fires.
3. Auto-probe scheduled when CB transitions OPEN → HALF_OPEN.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ── Classifier ───────────────────────────────────────────────────────


def test_caller_side_classifier_catches_orphan_tool_call():
    from app.routing.circuit_breaker import is_caller_side_error
    assert is_caller_side_error(
        "litellm.APIConnectionError: Missing corresponding tool call for tool response message"
    )


def test_caller_side_classifier_catches_cursor_bridge_string_expected():
    from app.routing.circuit_breaker import is_caller_side_error
    assert is_caller_side_error("Error: request.messages.content: string expected")


def test_caller_side_classifier_catches_openai_invalid_user_message():
    from app.routing.circuit_breaker import is_caller_side_error
    assert is_caller_side_error("Invalid user message at index 3")


def test_caller_side_classifier_misses_real_upstream_errors():
    """Connection refused, 500 ISE, gateway timeout etc. are real
    upstream failures — must NOT match the caller-side classifier."""
    from app.routing.circuit_breaker import is_caller_side_error
    assert not is_caller_side_error("OpenAIException - Connection error")
    assert not is_caller_side_error("HTTP 500 Internal Server Error")
    assert not is_caller_side_error("upstream returned 504 gateway timeout")
    assert not is_caller_side_error("")
    assert not is_caller_side_error(None)  # type: ignore[arg-type]


def test_caller_side_classifier_misses_auth_errors():
    """Auth errors route through is_auth_error path; must NOT also
    match caller-side."""
    from app.routing.circuit_breaker import is_caller_side_error
    assert not is_caller_side_error("authentication_error: invalid token")
    assert not is_caller_side_error("401 Unauthorized")


# ── record_outcome wiring ────────────────────────────────────────────


def test_record_outcome_skips_cb_on_caller_side():
    """Verify the source-level wiring: helpers.py routes through
    is_caller_side_error before calling record_failure."""
    src = Path("app/monitoring/helpers.py").read_text()
    assert "from app.routing.circuit_breaker import is_caller_side_error" in src
    assert "_caller_side = is_caller_side_error(error_str)" in src
    # The skip branch must precede the record_failure call so that
    # caller-side errors don't increment the CB.
    skip_idx = src.find("elif _caller_side:")
    fail_idx = src.find("await record_failure(provider_id, billing_error=is_billing_error")
    assert skip_idx != -1 and fail_idx != -1
    assert skip_idx < fail_idx, (
        "caller-side skip must come before the record_failure fallback"
    )


# ── Auto-probe scheduling ────────────────────────────────────────────


def test_schedule_auto_probe_helper_exists():
    from app.routing.circuit_breaker import _schedule_auto_probe
    assert callable(_schedule_auto_probe)


@pytest.mark.asyncio
async def test_get_state_schedules_auto_probe_on_hold_down_expiry():
    """When a CB's hold-down has expired and get_state() flips it to
    HALF_OPEN, an auto-probe must be scheduled. Locks the wiring in
    the state-machine transition site."""
    import time
    from app.routing import circuit_breaker as cb
    from app.config import settings

    pid = "test-auto-probe-pid"
    # Force the state into OPEN with an expired hold-down so the
    # half-open transition fires.
    state = cb._get_local(pid)
    state.state = cb.CBState.OPEN
    state.opened_at = time.time() - settings.circuit_breaker_timeout_sec - 10
    state.hold_down_until = time.time() - 5  # already expired

    with patch.object(cb, "_schedule_auto_probe") as m:
        new_state = await cb.get_state(pid)
    assert new_state == cb.CBState.HALF_OPEN
    m.assert_called_once_with(pid)


@pytest.mark.asyncio
async def test_get_state_does_not_schedule_probe_when_holding_down():
    """If the hold-down hasn't expired, we should stay in OPEN and NOT
    schedule a probe (the existing keepalive worker handles that case
    separately)."""
    import time
    from app.routing import circuit_breaker as cb

    pid = "test-still-holding-pid"
    state = cb._get_local(pid)
    state.state = cb.CBState.OPEN
    state.opened_at = time.time()  # just opened
    state.hold_down_until = time.time() + 60  # still holding

    with patch.object(cb, "_schedule_auto_probe") as m:
        new_state = await cb.get_state(pid)
    # State stays OPEN; probe not scheduled
    assert new_state == cb.CBState.OPEN
    m.assert_not_called()


# ── Hysteresis already exists (locked) ──────────────────────────────


def test_circuit_breaker_success_needed_is_at_least_two():
    """Pin: don't regress to 1. Hysteresis requires 2+ consecutive
    successes in HALF_OPEN to close — prevents flapping where one
    lucky success closes the CB only to fail the next call."""
    from app.config import settings
    assert settings.circuit_breaker_success_needed >= 2
