"""Run state machine — pure functions, no IO.

The Run lifecycle (per spec B.1):

    queued → running → requires_tool → running → ... → completed
                    ↘ failed
                    ↘ expired (deadline_ts exceeded)
                    ↘ cancelled (client-requested)

Every transition is explicit. ``advance(state, event)`` returns the new
state or raises ``InvalidTransition`` (which the API layer maps to 409
Conflict). All state is data — this module touches no DB, no asyncio,
no clock; ``deadline_ts`` is compared against an injected ``now`` so
tests are deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    REQUIRES_TOOL = "requires_tool"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (
            RunStatus.COMPLETED, RunStatus.FAILED,
            RunStatus.EXPIRED, RunStatus.CANCELLED,
        )


class ErrorKind(str, Enum):
    """Maps to ``error.kind`` on the ``failed`` event payload (spec)."""
    PROVIDER = "error_provider"            # all providers exhausted
    TOOL_LOOP_EXCEEDED = "tool_loop_exceeded"
    CONTEXT_EXHAUSTED = "context_exhausted"
    BAD_REQUEST = "bad_request"            # validation failure mid-run
    INTERNAL = "internal"                  # unexpected; preserve detail


class EventKind(str, Enum):
    """Inputs that drive the FSM. Maps roughly to spec B.6 events but
    these are the *internal* event-types the worker posts at the FSM;
    the wire-event names are derived (see app/runs/events.py in R4)."""
    START = "start"                              # queued → running
    MODEL_RETURNED_TEXT = "model_returned_text"  # running → completed (terminal text)
    MODEL_RETURNED_TOOL = "model_returned_tool"  # running → requires_tool
    TOOL_RESULT = "tool_result"                  # requires_tool → running
    PROVIDER_EXHAUSTED = "provider_exhausted"    # running → failed (kind=PROVIDER)
    MAX_TURNS_HIT = "max_turns_hit"              # running → failed (kind=TOOL_LOOP_EXCEEDED)
    CONTEXT_EXHAUSTED = "context_exhausted"      # running → failed (kind=CONTEXT_EXHAUSTED)
    INTERNAL_ERROR = "internal_error"            # any non-terminal → failed (kind=INTERNAL)
    DEADLINE_EXCEEDED = "deadline_exceeded"      # any non-terminal → expired
    CANCEL = "cancel"                            # any non-terminal → cancelled


class InvalidTransition(Exception):
    """Raised by ``advance`` when an event is not legal for the current state.

    The API layer translates this into HTTP 409 Conflict per spec
    "Failure modes the proxy team must handle".
    """
    def __init__(self, state: RunStatus, event: EventKind, reason: str = ""):
        self.state = state
        self.event = event
        msg = f"cannot {event.value} from {state.value}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


@dataclass
class RunCtx:
    """Just the FSM-relevant slice of a Run row.

    Kept narrow on purpose so unit tests can construct one literally.
    The persistence layer (R2) reads/writes the full Run row; the FSM
    only needs these fields.
    """
    status: RunStatus
    deadline_ts: float
    max_turns: int
    turns_used: int = 0           # how many user/tool→model round-trips so far
    error_kind: Optional[ErrorKind] = None
    error_message: Optional[str] = None


@dataclass
class Transition:
    """Result of an FSM step: new state + (optional) error metadata that
    must be persisted alongside the status change."""
    status: RunStatus
    turns_used: int
    error_kind: Optional[ErrorKind] = None
    error_message: Optional[str] = None
    extra: dict = field(default_factory=dict)


# Single source of truth for legal transitions. Lookup is by (status, event).
# Lambda receives (ctx, now, event_data) and returns Transition or raises.
def _start(ctx: RunCtx, now: float, _data: dict) -> Transition:
    if ctx.status is not RunStatus.QUEUED:
        raise InvalidTransition(ctx.status, EventKind.START)
    if now >= ctx.deadline_ts:
        # Deadline already past at start; spec demands transition straight to expired
        return Transition(RunStatus.EXPIRED, ctx.turns_used)
    return Transition(RunStatus.RUNNING, ctx.turns_used)


def _model_text(ctx: RunCtx, now: float, _data: dict) -> Transition:
    if ctx.status is not RunStatus.RUNNING:
        raise InvalidTransition(ctx.status, EventKind.MODEL_RETURNED_TEXT)
    if now >= ctx.deadline_ts:
        return Transition(RunStatus.EXPIRED, ctx.turns_used)
    return Transition(RunStatus.COMPLETED, ctx.turns_used + 1)


def _model_tool(ctx: RunCtx, now: float, _data: dict) -> Transition:
    if ctx.status is not RunStatus.RUNNING:
        raise InvalidTransition(ctx.status, EventKind.MODEL_RETURNED_TOOL)
    if now >= ctx.deadline_ts:
        return Transition(RunStatus.EXPIRED, ctx.turns_used)
    new_turns = ctx.turns_used + 1
    if new_turns >= ctx.max_turns:
        # Model wants another tool but we've exhausted the budget.
        # Spec: kind=tool_loop_exceeded; preserve final tool_use unsent.
        return Transition(
            RunStatus.FAILED, new_turns,
            error_kind=ErrorKind.TOOL_LOOP_EXCEEDED,
            error_message=f"max_turns={ctx.max_turns} reached",
        )
    return Transition(RunStatus.REQUIRES_TOOL, new_turns)


def _tool_result(ctx: RunCtx, now: float, _data: dict) -> Transition:
    if ctx.status is not RunStatus.REQUIRES_TOOL:
        raise InvalidTransition(
            ctx.status, EventKind.TOOL_RESULT,
            "run is not awaiting a tool result",
        )
    if now >= ctx.deadline_ts:
        return Transition(RunStatus.EXPIRED, ctx.turns_used)
    return Transition(RunStatus.RUNNING, ctx.turns_used)


def _provider_exhausted(ctx: RunCtx, _now: float, data: dict) -> Transition:
    if ctx.status not in (RunStatus.RUNNING, RunStatus.REQUIRES_TOOL):
        raise InvalidTransition(ctx.status, EventKind.PROVIDER_EXHAUSTED)
    return Transition(
        RunStatus.FAILED, ctx.turns_used,
        error_kind=ErrorKind.PROVIDER,
        error_message=data.get("detail") or "all providers exhausted",
    )


def _max_turns(ctx: RunCtx, _now: float, _data: dict) -> Transition:
    if ctx.status is not RunStatus.RUNNING:
        raise InvalidTransition(ctx.status, EventKind.MAX_TURNS_HIT)
    return Transition(
        RunStatus.FAILED, ctx.turns_used,
        error_kind=ErrorKind.TOOL_LOOP_EXCEEDED,
        error_message=f"max_turns={ctx.max_turns} reached",
    )


def _context_exhausted(ctx: RunCtx, _now: float, _data: dict) -> Transition:
    if ctx.status not in (RunStatus.RUNNING, RunStatus.REQUIRES_TOOL):
        raise InvalidTransition(ctx.status, EventKind.CONTEXT_EXHAUSTED)
    return Transition(
        RunStatus.FAILED, ctx.turns_used,
        error_kind=ErrorKind.CONTEXT_EXHAUSTED,
        error_message="context window exceeded even after compaction",
    )


def _internal(ctx: RunCtx, _now: float, data: dict) -> Transition:
    if ctx.status.terminal:
        raise InvalidTransition(ctx.status, EventKind.INTERNAL_ERROR)
    return Transition(
        RunStatus.FAILED, ctx.turns_used,
        error_kind=ErrorKind.INTERNAL,
        error_message=data.get("detail") or "internal error",
    )


def _deadline(ctx: RunCtx, _now: float, _data: dict) -> Transition:
    if ctx.status.terminal:
        raise InvalidTransition(ctx.status, EventKind.DEADLINE_EXCEEDED)
    return Transition(RunStatus.EXPIRED, ctx.turns_used)


def _cancel(ctx: RunCtx, _now: float, _data: dict) -> Transition:
    # Cancel is idempotent — if already cancelled, return the same state.
    # If terminal in another way, the spec says cancel is a no-op (but we
    # still return the current state so the API can stay idempotent).
    if ctx.status is RunStatus.CANCELLED:
        return Transition(RunStatus.CANCELLED, ctx.turns_used)
    if ctx.status.terminal:
        # Already done; don't fight it. API returns the existing state.
        return Transition(ctx.status, ctx.turns_used,
                          error_kind=ctx.error_kind,
                          error_message=ctx.error_message)
    return Transition(RunStatus.CANCELLED, ctx.turns_used)


_HANDLERS = {
    EventKind.START: _start,
    EventKind.MODEL_RETURNED_TEXT: _model_text,
    EventKind.MODEL_RETURNED_TOOL: _model_tool,
    EventKind.TOOL_RESULT: _tool_result,
    EventKind.PROVIDER_EXHAUSTED: _provider_exhausted,
    EventKind.MAX_TURNS_HIT: _max_turns,
    EventKind.CONTEXT_EXHAUSTED: _context_exhausted,
    EventKind.INTERNAL_ERROR: _internal,
    EventKind.DEADLINE_EXCEEDED: _deadline,
    EventKind.CANCEL: _cancel,
}


def advance(ctx: RunCtx, event: EventKind, now: float, data: Optional[dict] = None) -> Transition:
    """Compute the next state for ``ctx`` given an incoming ``event``.

    Raises ``InvalidTransition`` for illegal combinations. Pure: no IO,
    no global state. ``now`` is injected so tests can pin time and the
    deadline check stays deterministic.
    """
    handler = _HANDLERS.get(event)
    if handler is None:
        raise ValueError(f"unknown event kind: {event!r}")
    return handler(ctx, now, data or {})


# ── Helpers callers (API layer + worker) need ────────────────────────────────

def clamp_max_turns(requested: int, default_ceiling: int, hard_ceiling: int) -> tuple[int, bool]:
    """Apply Q6 lock-in: default ceiling 50, hard ceiling 200.

    Returns ``(clamped_value, was_clamped)``. The API emits a
    ``max_turns_clamped`` event when ``was_clamped`` is True.
    """
    if requested < 1:
        return 1, requested != 1
    capped_at = min(default_ceiling, hard_ceiling)
    if requested > capped_at:
        return capped_at, True
    return requested, False
