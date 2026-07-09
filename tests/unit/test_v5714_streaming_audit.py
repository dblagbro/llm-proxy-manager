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


def test_streaming_guard_imports_log_event():
    """Static-grep contract: the streaming guard imports log_event."""
    src = Path("app/api/_messages_streaming.py").read_text()
    assert "from app.monitoring.activity import log_event" in src, (
        "v5.7.14: stream_with_empty_guard must import log_event to "
        "persist empty-success failovers to activity_log."
    )


def test_streaming_guard_audits_failover_attempt():
    """Each failover attempt writes a streaming.empty_success_failover row."""
    src = Path("app/api/_messages_streaming.py").read_text()
    assert 'event_type="streaming.empty_success_failover"' in src
    # And it carries the provider id so the supervisor can group by provider.
    assert "provider_id=attempt_route.provider.id" in src


def test_streaming_guard_audits_terminal_exhaust():
    """When all candidates empty-fail, a streaming.failover_exhausted row
    lands BEFORE the 502 raise — so the audit survives the exception."""
    src = Path("app/api/_messages_streaming.py").read_text()
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
    src = Path("app/api/_messages_streaming.py").read_text()
    # Both log_event invocations live in try blocks.
    failover_idx = src.find('event_type="streaming.empty_success_failover"')
    exhaust_idx = src.find('event_type="streaming.failover_exhausted"')
    # Look backwards ~600 chars for the enclosing try; both must have one.
    for label, idx in [("failover", failover_idx), ("exhaust", exhaust_idx)]:
        window = src[max(0, idx - 600): idx]
        assert "try:" in window, f"v5.7.14: {label} audit not wrapped in try/except"


def test_version_bumped_to_5_7_14():
    """v5.7.14 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 14), f"v5.7.14 must be reachable; got {__version__}"
