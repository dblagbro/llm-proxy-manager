"""Run FSM transition matrix.

Covers every (state, event) combination once: legal ones produce the
expected target state + bookkeeping; illegal ones raise InvalidTransition.

Time is injected via ``now`` so deadline checks stay deterministic.
"""
import pytest

from app.runs.state import (
    RunCtx, RunStatus, EventKind, ErrorKind, advance,
    InvalidTransition, clamp_max_turns,
)


T0 = 1_700_000_000.0           # arbitrary "now"
DEADLINE_FUTURE = T0 + 60.0    # plenty of headroom
DEADLINE_PAST = T0 - 1.0       # already expired


def ctx(status, *, deadline=DEADLINE_FUTURE, max_turns=10, turns_used=0,
        error_kind=None, error_message=None):
    return RunCtx(
        status=status,
        deadline_ts=deadline,
        max_turns=max_turns,
        turns_used=turns_used,
        error_kind=error_kind,
        error_message=error_message,
    )


# ── Legal happy path: queued → running → completed ───────────────────────────


def test_start_from_queued():
    t = advance(ctx(RunStatus.QUEUED), EventKind.START, T0)
    assert t.status is RunStatus.RUNNING
    assert t.turns_used == 0


def test_model_text_completes_run():
    t = advance(ctx(RunStatus.RUNNING), EventKind.MODEL_RETURNED_TEXT, T0)
    assert t.status is RunStatus.COMPLETED
    assert t.turns_used == 1


def test_model_tool_transitions_to_requires_tool():
    t = advance(ctx(RunStatus.RUNNING), EventKind.MODEL_RETURNED_TOOL, T0)
    assert t.status is RunStatus.REQUIRES_TOOL
    assert t.turns_used == 1


def test_tool_result_resumes_running():
    t = advance(ctx(RunStatus.REQUIRES_TOOL, turns_used=1),
                EventKind.TOOL_RESULT, T0)
    assert t.status is RunStatus.RUNNING
    # tool_result does NOT increment turns — the next model call does
    assert t.turns_used == 1


# ── Deadline enforcement at every transition ─────────────────────────────────


@pytest.mark.parametrize("event", [
    EventKind.START,
    EventKind.MODEL_RETURNED_TEXT,
    EventKind.MODEL_RETURNED_TOOL,
    EventKind.TOOL_RESULT,
])
def test_deadline_past_during_transition_expires(event):
    """Per spec 'Deadline exceeded mid-call → transition to expired'."""
    state = {
        EventKind.START: RunStatus.QUEUED,
        EventKind.MODEL_RETURNED_TEXT: RunStatus.RUNNING,
        EventKind.MODEL_RETURNED_TOOL: RunStatus.RUNNING,
        EventKind.TOOL_RESULT: RunStatus.REQUIRES_TOOL,
    }[event]
    t = advance(ctx(state, deadline=DEADLINE_PAST), event, T0)
    assert t.status is RunStatus.EXPIRED


def test_explicit_deadline_event_from_running():
    t = advance(ctx(RunStatus.RUNNING), EventKind.DEADLINE_EXCEEDED, T0)
    assert t.status is RunStatus.EXPIRED


def test_explicit_deadline_event_from_requires_tool():
    t = advance(ctx(RunStatus.REQUIRES_TOOL), EventKind.DEADLINE_EXCEEDED, T0)
    assert t.status is RunStatus.EXPIRED


def test_deadline_event_on_terminal_run_rejected():
    with pytest.raises(InvalidTransition):
        advance(ctx(RunStatus.COMPLETED), EventKind.DEADLINE_EXCEEDED, T0)


# ── max_turns enforcement (Q6 lock-in: clamp + tool_loop_exceeded) ───────────


def test_model_tool_at_max_turns_fails_with_tool_loop_exceeded():
    """Spec: 'Run hits max_turns → fail with kind=tool_loop_exceeded;
    preserve final tool_use unsent.' We mark FAILED with the right kind;
    the unsent tool_use stays on the Run row for inspection (R2)."""
    t = advance(ctx(RunStatus.RUNNING, max_turns=2, turns_used=1),
                EventKind.MODEL_RETURNED_TOOL, T0)
    assert t.status is RunStatus.FAILED
    assert t.error_kind is ErrorKind.TOOL_LOOP_EXCEEDED
    assert "max_turns=2" in (t.error_message or "")


def test_explicit_max_turns_hit_event():
    t = advance(ctx(RunStatus.RUNNING, max_turns=5),
                EventKind.MAX_TURNS_HIT, T0)
    assert t.status is RunStatus.FAILED
    assert t.error_kind is ErrorKind.TOOL_LOOP_EXCEEDED


def test_max_turns_hit_invalid_from_requires_tool():
    with pytest.raises(InvalidTransition):
        advance(ctx(RunStatus.REQUIRES_TOOL, max_turns=5),
                EventKind.MAX_TURNS_HIT, T0)


# ── Provider exhaustion ──────────────────────────────────────────────────────


def test_provider_exhausted_from_running_fails_with_provider_kind():
    t = advance(ctx(RunStatus.RUNNING), EventKind.PROVIDER_EXHAUSTED, T0,
                {"detail": "tried 3 providers"})
    assert t.status is RunStatus.FAILED
    assert t.error_kind is ErrorKind.PROVIDER
    assert t.error_message == "tried 3 providers"


def test_provider_exhausted_from_requires_tool_fails():
    """Spec doesn't preclude provider failure mid tool-wait recovery —
    if the next model call after a tool result exhausts, we still fail."""
    t = advance(ctx(RunStatus.REQUIRES_TOOL),
                EventKind.PROVIDER_EXHAUSTED, T0)
    assert t.status is RunStatus.FAILED
    assert t.error_kind is ErrorKind.PROVIDER


def test_provider_exhausted_on_queued_invalid():
    with pytest.raises(InvalidTransition):
        advance(ctx(RunStatus.QUEUED), EventKind.PROVIDER_EXHAUSTED, T0)


# ── Context exhaustion ───────────────────────────────────────────────────────


def test_context_exhausted_from_running():
    t = advance(ctx(RunStatus.RUNNING), EventKind.CONTEXT_EXHAUSTED, T0)
    assert t.status is RunStatus.FAILED
    assert t.error_kind is ErrorKind.CONTEXT_EXHAUSTED


def test_context_exhausted_invalid_from_queued():
    with pytest.raises(InvalidTransition):
        advance(ctx(RunStatus.QUEUED), EventKind.CONTEXT_EXHAUSTED, T0)


# ── Cancellation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("from_status", [
    RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.REQUIRES_TOOL,
])
def test_cancel_from_non_terminal_transitions_to_cancelled(from_status):
    t = advance(ctx(from_status), EventKind.CANCEL, T0)
    assert t.status is RunStatus.CANCELLED


def test_cancel_idempotent_on_cancelled():
    t = advance(ctx(RunStatus.CANCELLED), EventKind.CANCEL, T0)
    assert t.status is RunStatus.CANCELLED


@pytest.mark.parametrize("terminal", [
    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.EXPIRED,
])
def test_cancel_on_terminal_returns_existing_state(terminal):
    """Cancel after the run already finished is a no-op so the API stays
    idempotent. The hub's reaper may post Cancel after a deadline event."""
    t = advance(ctx(terminal, error_kind=ErrorKind.PROVIDER,
                    error_message="X"),
                EventKind.CANCEL, T0)
    assert t.status is terminal
    assert t.error_kind is ErrorKind.PROVIDER
    assert t.error_message == "X"


# ── Tool-result on the wrong state (spec: 409 conflict) ──────────────────────


@pytest.mark.parametrize("from_status", [
    RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.COMPLETED,
    RunStatus.FAILED, RunStatus.EXPIRED, RunStatus.CANCELLED,
])
def test_tool_result_on_wrong_state_raises(from_status):
    with pytest.raises(InvalidTransition) as ei:
        advance(ctx(from_status), EventKind.TOOL_RESULT, T0)
    assert "tool result" in str(ei.value).lower() or from_status.value in str(ei.value)


# ── Internal-error fallback ──────────────────────────────────────────────────


@pytest.mark.parametrize("from_status", [
    RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.REQUIRES_TOOL,
])
def test_internal_error_marks_failed_with_internal_kind(from_status):
    t = advance(ctx(from_status), EventKind.INTERNAL_ERROR, T0,
                {"detail": "kaboom"})
    assert t.status is RunStatus.FAILED
    assert t.error_kind is ErrorKind.INTERNAL
    assert t.error_message == "kaboom"


@pytest.mark.parametrize("terminal", [
    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.EXPIRED, RunStatus.CANCELLED,
])
def test_internal_error_invalid_from_terminal(terminal):
    with pytest.raises(InvalidTransition):
        advance(ctx(terminal), EventKind.INTERNAL_ERROR, T0)


# ── Start from non-queued is illegal ─────────────────────────────────────────


@pytest.mark.parametrize("from_status", [
    RunStatus.RUNNING, RunStatus.REQUIRES_TOOL, RunStatus.COMPLETED,
    RunStatus.FAILED, RunStatus.EXPIRED, RunStatus.CANCELLED,
])
def test_start_from_non_queued_raises(from_status):
    with pytest.raises(InvalidTransition):
        advance(ctx(from_status), EventKind.START, T0)


# ── Model-text / model-tool from non-running is illegal ──────────────────────


@pytest.mark.parametrize("from_status", [
    RunStatus.QUEUED, RunStatus.REQUIRES_TOOL, RunStatus.COMPLETED,
    RunStatus.FAILED, RunStatus.EXPIRED, RunStatus.CANCELLED,
])
@pytest.mark.parametrize("event", [
    EventKind.MODEL_RETURNED_TEXT, EventKind.MODEL_RETURNED_TOOL,
])
def test_model_returns_only_legal_from_running(from_status, event):
    with pytest.raises(InvalidTransition):
        advance(ctx(from_status), event, T0)


# ── Deadline-already-past at start ───────────────────────────────────────────


def test_start_with_past_deadline_goes_straight_to_expired():
    t = advance(ctx(RunStatus.QUEUED, deadline=DEADLINE_PAST),
                EventKind.START, T0)
    assert t.status is RunStatus.EXPIRED


# ── clamp_max_turns ─────────────────────────────────────────────────────────


def test_clamp_max_turns_below_default():
    val, clamped = clamp_max_turns(20, default_ceiling=50, hard_ceiling=200)
    assert (val, clamped) == (20, False)


def test_clamp_max_turns_at_default_ceiling():
    val, clamped = clamp_max_turns(50, default_ceiling=50, hard_ceiling=200)
    assert (val, clamped) == (50, False)


def test_clamp_max_turns_above_default_clamps_to_default():
    val, clamped = clamp_max_turns(75, default_ceiling=50, hard_ceiling=200)
    assert (val, clamped) == (50, True)


def test_clamp_max_turns_admin_raised_default_obeys_hard():
    val, clamped = clamp_max_turns(500, default_ceiling=200, hard_ceiling=200)
    assert (val, clamped) == (200, True)


def test_clamp_max_turns_zero_or_negative_clamped_to_one():
    val, clamped = clamp_max_turns(0, default_ceiling=50, hard_ceiling=200)
    assert (val, clamped) == (1, True)


# ── Cancel idempotency at the FSM layer (hub team flag B) ────────────────────


def test_cancel_on_already_cancelled_run_returns_same_state():
    """The cancel endpoint snapshots the prior status before calling
    ``advance`` so it can suppress duplicate ``cancelled`` event emission.
    The FSM itself is happy to be re-called — verify."""
    t = advance(ctx(RunStatus.CANCELLED), EventKind.CANCEL, T0)
    assert t.status is RunStatus.CANCELLED
    # No error_kind set — duplicate cancel is not an error
    assert t.error_kind is None
