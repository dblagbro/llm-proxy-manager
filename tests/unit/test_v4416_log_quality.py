"""v4.4.16 log-quality fixes from the 2026-05-22 log audit.

Two findings, same theme as v4.4.13's cluster-sync log fix:

A. ``cluster/manager.py`` heartbeat path logged
   ``f"Cluster peer {peer.id} unreachable: {e}"`` — and ``{e}`` is
   empty for ``httpx.ReadTimeout()`` / ``ConnectError("")``, so the
   line rendered as "...unreachable: " with no diagnostic. This is
   the sibling of the push_sync line fixed in v4.4.13 (which was
   missed because it's a different function). Fixed the same way:
   surface ``type(e).__name__`` + a non-empty message.

B. ``auth/admin.py`` logged ``session_not_found`` + ``session_expired``
   at WARNING. Session expiry is an EXPECTED condition (stale browser
   cookie → 401 → re-login), not an anomaly. The audit found 207
   identical ``session_not_found`` lines in 12h from ONE dead cookie,
   burying real warnings. Downgraded both to DEBUG — the 401 response
   is the real signal; the log line is noise.
"""
from __future__ import annotations

from pathlib import Path


# ── A: cluster-peer-unreachable empty-exception fix ──────────────


def test_unreachable_log_handles_empty_exception():
    src = Path("app/cluster/manager.py").read_text()
    # Locate the unreachable log emission
    idx = src.index('Cluster peer %s unreachable')
    window = src[idx - 300:idx + 200]
    # Uses type name + non-empty fallback (same pattern as push_sync)
    assert "type(e).__name__" in window
    assert "(no message)" in window
    # The old bare f-string form must be gone
    assert 'f"Cluster peer {peer.id} unreachable: {e}"' not in src


def test_unreachable_fallback_behavior():
    """Behavioral: replicate the inline fallback for an empty-message
    exception (the logic lives inline in the except block)."""
    import httpx
    e = httpx.ConnectError("")
    msg = str(e) if str(e) else f"{type(e).__name__} (no message)"
    assert msg == "ConnectError (no message)"
    e2 = httpx.ConnectError("Name or service not known")
    msg2 = str(e2) if str(e2) else f"{type(e2).__name__} (no message)"
    assert msg2 == "Name or service not known"


# ── B: session-log level downgrade ───────────────────────────────


def test_session_not_found_logged_at_debug():
    src = Path("app/auth/admin.py").read_text()
    idx = src.index("session_not_found")
    # The logger call on/around that line must be .debug, not .warning
    line_start = src.rfind("logger.", 0, idx)
    call = src[line_start:idx + 40]
    assert "logger.debug(" in call, (
        "session_not_found should log at DEBUG (expected condition), not WARNING"
    )


def test_session_expired_logged_at_debug():
    src = Path("app/auth/admin.py").read_text()
    idx = src.index("session_expired")
    line_start = src.rfind("logger.", 0, idx)
    call = src[line_start:idx + 40]
    assert "logger.debug(" in call, (
        "session_expired should log at DEBUG (normal lifecycle), not WARNING"
    )


def test_no_warning_level_session_logs_remain():
    """Neither session log line should be at WARNING anymore."""
    src = Path("app/auth/admin.py").read_text()
    assert 'logger.warning("session_not_found' not in src
    assert 'logger.warning("session_expired' not in src
