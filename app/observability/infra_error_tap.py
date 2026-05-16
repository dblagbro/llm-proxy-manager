"""v3.10.15 BUG-032 — tap ASGI + SQLAlchemy connection-pool error logs.

ASGI exceptions (client-disconnect ``CancelledError`` / "Connection
closed") and ``sqlalchemy.pool`` GC errors used to log as bare stdlib
``ERROR`` records: they never reached ``activity_log``, so the v3.10.4
error-rate alert was blind to them, and a benign client-disconnect was
indistinguishable from a genuine ASGI crash in the logs.

This module attaches a lightweight ``logging.Handler`` to those two
loggers. The handler does not re-log anything — it only **classifies**
each record (benign disconnect vs genuine fault) and increments the
``llm_proxy_infra_errors_total`` Prometheus counter, so the errors are
visible on ``/metrics`` and a fault spike is distinguishable from
routine client churn.
"""
from __future__ import annotations

import logging

# Substrings (lower-cased match) that mark a benign client-disconnect
# rather than a genuine server-side fault.
_DISCONNECT_MARKERS = (
    "cancellederror",
    "connectionreseterror",
    "connection closed",
    "connection lost",
    "connection aborted",
    "clientdisconnect",
    "client disconnect",
    "broken pipe",
    "brokenpipeerror",
    "errno 32",  # EPIPE
    "incomplete read",
)


def classify_fault(text: str) -> str:
    """``"disconnect"`` for a benign client-side disconnect, else
    ``"fault"`` (a genuine server-side / pool fault)."""
    low = (text or "").lower()
    return "disconnect" if any(m in low for m in _DISCONNECT_MARKERS) else "fault"


class _InfraErrorTap(logging.Handler):
    """Counts (never re-emits) infra error log records for one source."""

    def __init__(self, source: str, min_level: int):
        super().__init__(level=min_level)
        self._source = source

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from app.observability.prometheus import observe_infra_error

            text = record.getMessage()
            if record.exc_info:
                try:
                    text += " " + logging.Formatter().formatException(record.exc_info)
                except Exception:
                    pass
            observe_infra_error(self._source, classify_fault(text))
        except Exception:
            # A logging handler must never raise — swallow everything.
            pass


_installed = False


def install_infra_error_tap() -> None:
    """Attach the tap to the SQLAlchemy-pool and ASGI error loggers.
    Idempotent — safe to call more than once."""
    global _installed
    if _installed:
        return
    # Pool issues surface at WARNING+ (the GC "non-checked-in connection"
    # message) and ERROR ("Exception terminating connection").
    logging.getLogger("sqlalchemy.pool").addHandler(
        _InfraErrorTap("pool", logging.WARNING)
    )
    # ASGI crashes are logged at ERROR by uvicorn's error logger.
    logging.getLogger("uvicorn.error").addHandler(
        _InfraErrorTap("asgi", logging.ERROR)
    )
    _installed = True
