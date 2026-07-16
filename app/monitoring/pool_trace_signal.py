"""v5.21.6 — SIGUSR2 handler dumps the DB-pool trace to container logs.

Chronic outage since 2026-07-09: tmrwww01 and tmrwww02 llm-proxy2 pool
exhausts every 24-48h and login stops working. Root cause unknown
because forensics require admin session cookies — which requires
login — which requires the pool — which is exhausted. Chicken/egg.

This handler breaks the loop. Any of these commands from a shell that
can ``docker kill`` the container produces the trace in stdout logs
without needing to log in:

    docker kill --signal=SIGUSR2 llm-proxy2
    sudo docker kill --signal=SIGUSR2 llm-proxy2

The dump lists every currently-held async session with the CALLER's
stack (captured at ``AsyncSession.__aenter__``). Sorted oldest-first
so leaked sessions bubble to the top.

Wired at app startup — see ``main.py`` ``lifespan``.
"""
from __future__ import annotations

import logging
import signal
from typing import Any

logger = logging.getLogger(__name__)


def _dump_pool_trace(_signum: int, _frame: Any) -> None:
    """Log the current async-session trace + sync-pool trace to stdout.
    Called in the main asyncio thread on SIGUSR2. Best-effort — never
    raises (a SIGUSR2 handler that raises would kill the process).
    """
    try:
        from app.models.database import (
            get_async_session_trace, get_pool_checkout_trace,
        )
        async_sessions = get_async_session_trace()
        pool_checkouts = get_pool_checkout_trace()

        logger.info(
            "SIGUSR2 db-pool dump: async_sessions=%d sync_checkouts=%d",
            len(async_sessions), len(pool_checkouts),
        )

        top_n = 20
        for i, entry in enumerate(async_sessions[:top_n]):
            stack = entry.get("stack", "")
            app_frames = [
                line.strip() for line in stack.split("\n")
                if "/app/" in line or "app/api" in line or "app/routing" in line
                or "app/monitoring" in line or "app/models" in line
            ]
            leaf_frames = app_frames[-6:] if len(app_frames) > 6 else app_frames
            logger.info(
                "  #%d age=%.1fs session_id=%s",
                i, entry.get("age_sec", 0.0),
                entry.get("session_id", "?")[:12],
            )
            for line in leaf_frames:
                logger.info("      %s", line[:160])
        if len(async_sessions) > top_n:
            logger.info(
                "  … +%d more async sessions (dump capped at %d)",
                len(async_sessions) - top_n, top_n,
            )
    except Exception as exc:
        logger.warning("SIGUSR2 dump handler failed: %r", exc)


def install_pool_trace_signal_handler() -> bool:
    """Install SIGUSR2 -> dump-pool-trace. Idempotent. Returns True if
    installed, False if the signal isn't available on this platform
    (Windows) or if installation raised.

    Safe to call at app boot even when ``db_pool_trace`` is off - the
    dump just shows ``async_sessions=0`` and terminates cleanly. Cheap
    at rest.
    """
    try:
        if not hasattr(signal, "SIGUSR2"):
            return False
        signal.signal(signal.SIGUSR2, _dump_pool_trace)
        logger.info(
            "SIGUSR2 handler installed - send 'docker kill --signal=SIGUSR2 llm-proxy2' "
            "to dump the current DB-pool trace to these logs."
        )
        return True
    except Exception as exc:
        logger.warning("failed to install SIGUSR2 handler: %r", exc)
        return False
