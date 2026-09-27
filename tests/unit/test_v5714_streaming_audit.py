"""v5.7.14 — empty-success failover events land in activity_log.

Pre-5.7.14, ``stream_with_empty_guard`` only logged failovers via
``logger.warning``. The supervisor (which polls the DB) and the
operator dashboard's recent-events panel never saw them, so the
operator's escalation about "be more active" had no fuel — the
supervisor literally could not detect bursts because the bursts
weren't audited.

5.7.14 lands two audit rows:

  - ``streaming.empty_success_failover`` — one per failover attempt
  - ``streaming.failover_exhausted``    — once when the 502 fires

The write goes through ``app.monitoring.activity.log_event`` so the
existing logging-controls kill-switch still gates it.
"""
from __future__ import annotations

from pathlib import Path

# v5.22.36 — ``stream_with_empty_guard`` and the SSE frame helpers moved from
# ``app/api/_messages_streaming.py`` into ``app/api/_sse_guard.py`` when the
# parent was split to get back under its 700-LOC ceiling. These are source-grep
# guards, so they have to follow the code; the behaviour they pin is unchanged
# and importers still reach the function through the parent's re-export shim.
_GUARD_SRC = Path("app/api/_sse_guard.py")

def test_streaming_guard_imports_log_event():
    """Static-grep contract: the streaming guard imports log_event."""
    src = _GUARD_SRC.read_text()
    assert "from app.monitoring.activity import log_event" in src, (
        "v5.7.14: stream_with_empty_guard must import log_event to "
        "persist empty-success failovers to activity_log."
    )


def test_streaming_guard_audits_failover_attempt():
    """Each failover attempt writes a streaming.empty_success_failover row."""
    src = _GUARD_SRC.read_text()
    assert 'event_type="streaming.empty_success_failover"' in src
    # And it carries the provider id so the supervisor can group by provider.
    assert "provider_id=attempt_route.provider.id" in src


def test_streaming_guard_audits_terminal_exhaust():
    """When all candidates empty-fail, a streaming.failover_exhausted row
    lands BEFORE the 502 raise — so the audit survives the exception."""
    src = _GUARD_SRC.read_text()
    exhaust_idx = src.find('event_type="streaming.failover_exhausted"')
    raise_502_idx = src.find('raise HTTPException(\n        502,')
    assert exhaust_idx != -1, "v5.7.14: terminal exhaust event missing"
    assert raise_502_idx != -1
    assert exhaust_idx < raise_502_idx, (
        "v5.7.14: failover_exhausted audit row must be written BEFORE "
        "the 502 raise — otherwise the exception path skips it."
    )


def test_audit_writes_are_exception_safe():
    """Audit writes must never break the failover loop. Both call sites
    are wrapped in try/except — a logging-controls flip mid-stream
    must not 502 a request that would otherwise succeed."""
    src = _GUARD_SRC.read_text()
    # Both log_event invocations live in try blocks.
    failover_idx = src.find('event_type="streaming.empty_success_failover"')
    exhaust_idx = src.find('event_type="streaming.failover_exhausted"')
    # Look backwards ~600 chars for the enclosing try; both must have one.
    #
    # v5.22.36 — the `idx != -1` assertion below is new. Without it a missing
    # event made `find()` return -1, so the window was `src[0:-1]` — nearly the
    # whole module, which always contains a "try:" somewhere. The test passed
    # whether or not the audit writes existed at all, and it kept passing when
    # the code moved out of this file entirely.
    for label, idx in [("failover", failover_idx), ("exhaust", exhaust_idx)]:
        assert idx != -1, (
            f"v5.7.14: {label} audit event not found at all — this test would "
            f"otherwise search the whole module and pass on an unrelated try:"
        )
        window = src[max(0, idx - 600): idx]
        assert "try:" in window, f"v5.7.14: {label} audit not wrapped in try/except"


def test_version_bumped_to_5_7_14():
    """v5.7.14 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 14), f"v5.7.14 must be reachable; got {__version__}"
