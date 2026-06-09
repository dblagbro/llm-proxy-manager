"""v5.3.4 — tap openai-python's transparent-retry INFO log line.

The openai-python http layer retries on its own (default 2 retries)
and absorbs the resulting transient upstream errors before they reach
``litellm``'s response object. From our application's view, a
``/v1/messages`` request that took 14 seconds with 3 underlying
upstream 429s + a final 200 looks identical to a clean 200 in 14
seconds — no row in ``activity_log``, no signal anywhere except a
suspicious latency tail.

Today's c1conv finding (2026-06-09 04:22-04:35 UTC): 14 retries
clustered in a 13-minute burst, completely invisible to our DB
audit. This module closes the gap by tapping the
``openai._base_client`` logger at INFO level, filtering for the
specific "Retrying request to <endpoint> in <N> seconds" message
template, and incrementing a Prometheus counter on every hit.

Also sets ``propagate=False`` on that logger so the retry messages
stop polluting stdout / ``docker logs`` (Ship B). The handler still
receives the records — propagation is to PARENT loggers, not local
handlers — so no signal is lost.

Modeled on ``app.observability.infra_error_tap``, which already
covered the SQLAlchemy-pool + ASGI cases with the same pattern
(2026-04-23 BUG-032). Same "logging handlers must never raise"
discipline.
"""
from __future__ import annotations

import logging
import re


# Matches openai-python 1.x's message template:
#   "Retrying request to /chat/completions in 0.381504 seconds"
# Captures the endpoint path. ``.+`` is intentionally permissive so a
# future openai-python rename of the template-fragment doesn't silently
# break the tap; the falsy fall-through hands a None ``endpoint`` to
# ``observe_openai_retry``, which collapses it to "other" in the label.
_RETRY_PATTERN = re.compile(r"Retrying request to (\S+) in")


class _OpenAIRetryTap(logging.Handler):
    """Counts (never re-emits) openai-python transparent retries."""

    def __init__(self):
        super().__init__(level=logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if "Retrying request" not in text:
                return
            m = _RETRY_PATTERN.search(text)
            endpoint = m.group(1) if m else None
            from app.observability.prometheus import observe_openai_retry
            observe_openai_retry(endpoint or "other")
        except Exception:
            # Logging handlers must never raise — swallow everything.
            pass


_installed = False


def install_openai_retry_tap() -> None:
    """Attach the tap to ``openai._base_client`` and stop the retry INFO
    chatter from leaking to stdout. Idempotent — safe to call more than
    once."""
    global _installed
    if _installed:
        return
    target = logging.getLogger("openai._base_client")
    target.addHandler(_OpenAIRetryTap())
    # Ship B — stop INFO retry messages from reaching the root logger
    # (which writes to stdout / docker logs). Local handlers — including
    # our tap above — still fire; propagation only controls the bubble-up.
    target.propagate = False
    # Make sure INFO records reach our handler — uvicorn / structlog may
    # have raised the level on parent loggers but not on this child.
    if target.level == logging.NOTSET or target.level > logging.INFO:
        target.setLevel(logging.INFO)
    _installed = True
