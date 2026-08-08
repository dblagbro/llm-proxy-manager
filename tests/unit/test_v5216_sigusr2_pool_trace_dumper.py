"""v5.21.6 — SIGUSR2 handler dumps DB-pool trace without needing login.

Chronic outage since 2026-07-09 (repeated 2026-07-14 + 2026-07-15):
tmrwww01/02 llm-proxy2 pool exhausts every 24-48h. Root cause
undiagnosable because the admin trace endpoint requires login and
login requires the pool. SIGUSR2 breaks the chicken/egg.
"""
from __future__ import annotations
from pathlib import Path


def test_signal_module_present():
    src_path = Path("app/monitoring/pool_trace_signal.py")
    assert src_path.exists()
    src = src_path.read_text()
    assert "def install_pool_trace_signal_handler" in src
    assert "def _dump_pool_trace" in src


def test_uses_sigusr2_not_sigusr1():
    """SIGUSR1 is often reserved by uvicorn/gunicorn for graceful
    reload. SIGUSR2 is the safer choice — no framework uses it."""
    src = Path("app/monitoring/pool_trace_signal.py").read_text()
    assert "signal.SIGUSR2" in src
    assert "signal.SIGUSR1" not in src


def test_handler_calls_both_trace_getters():
    """Both async and sync traces should be dumped — the async-side
    trace is where app code lives, the sync-side is what SQLAlchemy
    sees. Different failure modes surface in each."""
    src = Path("app/monitoring/pool_trace_signal.py").read_text()
    assert "get_async_session_trace" in src
    assert "get_pool_checkout_trace" in src


def test_handler_never_raises():
    """A signal handler that raises kills the process. This handler
    must be wrapped in try/except at the outermost level."""
    src = Path("app/monitoring/pool_trace_signal.py").read_text()
    # Find the handler function body
    start = src.find("def _dump_pool_trace")
    end = src.find("\ndef ", start + 10)
    body = src[start:end] if end > 0 else src[start:]
    assert "try:" in body
    assert "except Exception" in body


def test_handler_capped_at_top_n():
    """A leaked pool at 150 sessions would produce a huge dump. Cap it
    so the operator can actually read the output in docker logs."""
    src = Path("app/monitoring/pool_trace_signal.py").read_text()
    assert "top_n" in src or "TOP_N" in src


def test_install_idempotent_and_platform_safe():
    """Windows lacks SIGUSR2. Install must gracefully return False
    rather than raise, so the app boots on any platform."""
    src = Path("app/monitoring/pool_trace_signal.py").read_text()
    assert 'hasattr(signal, "SIGUSR2")' in src


def test_wired_into_lifespan():
    """The handler is useless if it isn't installed at boot. Verify
    the lifespan hook calls install."""
    src = Path("app/main.py").read_text()
    assert "install_pool_trace_signal_handler" in src


def test_install_actually_registers_the_signal():
    """End-to-end: importing + calling the installer must actually
    register a SIGUSR2 handler with the OS. If the installer no-ops
    silently (e.g. imports fail), the whole ship is useless."""
    import signal as _signal
    from app.monitoring.pool_trace_signal import install_pool_trace_signal_handler
    # Save prior handler to restore
    prior = _signal.getsignal(_signal.SIGUSR2)
    try:
        ok = install_pool_trace_signal_handler()
        assert ok is True, "installer returned False on a POSIX system"
        current = _signal.getsignal(_signal.SIGUSR2)
        assert current is not prior, "SIGUSR2 handler wasn't replaced"
        assert current is not _signal.SIG_DFL, "handler is still default"
    finally:
        _signal.signal(_signal.SIGUSR2, prior or _signal.SIG_DFL)


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 6), (
        f"expected >= 5.21.6, got {major}.{minor}.{patch}"
    )
